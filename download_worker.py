# -*- coding: utf-8 -*-
"""
Background worker that downloads tiles sequentially.

Design notes:
  - One file at a time (per the spec).
  - Cancel is "after current file" — checked between files only, so the in-
    flight download is allowed to finish cleanly.
  - Already-present files are skipped (size > 0 in the target location).
  - Each download retries once on network failure with a brief backoff before
    being recorded as failed.
  - Failures are written to failed_downloads.txt in the chosen output folder
    when the batch ends (whether it completed or was canceled).
  - Signals communicate progress back to the GUI thread without blocking.
  - For LiDAR jobs, the worker also downloads ONE metadata PDF per project
    into a Metadata/ subfolder under the project folder. If no PDF is
    available for a project, a metadata.txt placeholder is written instead.
"""

import os
import time
from urllib import request
from urllib.error import URLError, HTTPError

from qgis.PyQt.QtCore import QObject, pyqtSignal

from .version_filter import filename_from_url


CHUNK_SIZE = 64 * 1024  # 64 KB read chunks


class DownloadWorker(QObject):
    """QObject that runs in a QThread and processes a list of tile jobs.

    Signals:
      progress(int current, int total, str current_filename)
      message(str)                — informational status updates
      finished(int succeeded, int failed, int skipped, str failed_log_path)
    """

    progress = pyqtSignal(int, int, str)
    message = pyqtSignal(str)
    finished = pyqtSignal(int, int, int, str)

    def __init__(self, jobs, output_dir, parent=None):
        """
        :param jobs: list of dicts with keys:
            'url'      - download URL (required)
            'filename' - target filename
            'product'  - 'LiDAR' | 'DEM' | 'DSM'
            'scale'    - e.g. '1to2500'
            'layer_name' - source layer name (for logging)
            'object_id'  - source OBJECTID (for logging)
            'rpt_url'  - report/metadata PDF URL (LiDAR only; may be None)
            'project'  - project name (LiDAR only; may be None for "Unknown_Project")
        :param output_dir: root output directory (e.g. C:/lidar_downloads)
        """
        super().__init__(parent)
        self.jobs = jobs
        self.output_dir = output_dir
        self._cancel_requested = False

    def request_cancel(self):
        """Ask the worker to stop after the current download finishes."""
        self._cancel_requested = True

    def _target_path(self, job):
        """Compute the on-disk path for a job.

        All products use: <root>/<product>/<project>/<scale>/<filename>
          - LiDAR uses 'Project Name' attribute as project.
          - DEM/DSM use 'oper_name' attribute as project.
        The 'project' value on each job is the sanitized folder name
        (or 'Unknown_Project' if the attribute was empty).
        """
        project = job.get("project") or "Unknown_Project"
        return os.path.join(
            self.output_dir, job["product"], project, job["scale"], job["filename"]
        )

    def _download_one(self, url, target_path):
        """Download a single URL to target_path, atomically.

        Downloads to <target>.part and renames on success so a canceled
        or crashed download won't leave a partial file masquerading as
        a complete one.

        Returns (success: bool, error_msg: str or None).
        """
        partial = target_path + ".part"
        try:
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            req = request.Request(
                url, headers={"User-Agent": "LidarBC-QGIS-Plugin/1.0"}
            )
            with request.urlopen(req, timeout=120) as resp, open(partial, "wb") as out:
                while True:
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    out.write(chunk)
            # Successful: move into place
            if os.path.exists(target_path):
                os.remove(target_path)
            os.rename(partial, target_path)
            return True, None
        except HTTPError as e:
            msg = f"HTTP {e.code} {e.reason}"
        except URLError as e:
            msg = f"Network error: {e.reason}"
        except OSError as e:
            msg = f"Disk/IO error: {e}"
        except Exception as e:
            msg = f"Unexpected error: {e}"

        # Clean up partial file on failure
        try:
            if os.path.exists(partial):
                os.remove(partial)
        except OSError:
            pass
        return False, msg

    def _write_no_metadata_placeholder(self, metadata_dir):
        """Write a metadata.txt file noting that no metadata PDF was found."""
        try:
            os.makedirs(metadata_dir, exist_ok=True)
            placeholder = os.path.join(metadata_dir, "metadata.txt")
            # Don't overwrite if it already exists (preserves any user edits)
            if not os.path.exists(placeholder):
                with open(placeholder, "w", encoding="utf-8") as fh:
                    fh.write("No metadata was available for these files.\n")
        except OSError:
            pass

    def _collect_lidar_metadata_jobs(self):
        """For each LiDAR (project, scale), pick one rpt_url to download.

        We download ONE PDF per (project, scale) combination — the Metadata
        folder lives inside the scale folder, next to the .laz files it
        documents. For projects/scales with no rpt_url available, we'll write
        a metadata.txt placeholder instead.

        Returns:
          pdf_jobs: list of (metadata_dir, rpt_url, pdf_filename) tuples
          placeholder_dirs: list of metadata directories that need a placeholder
        """
        per_project_scale = {}  # (project, scale) -> first non-empty rpt_url
        all_keys = set()

        for job in self.jobs:
            if job.get("product") != "LiDAR":
                continue
            project = job.get("project") or "Unknown_Project"
            scale = job["scale"]
            key = (project, scale)
            all_keys.add(key)
            rpt = job.get("rpt_url")
            if rpt and key not in per_project_scale:
                per_project_scale[key] = rpt

        pdf_jobs = []
        placeholder_dirs = []
        for key in all_keys:
            project, scale = key
            metadata_dir = os.path.join(
                self.output_dir, "LiDAR", project, scale, "Metadata"
            )
            if key in per_project_scale:
                rpt_url = per_project_scale[key]
                pdf_filename = filename_from_url(rpt_url) or f"{project}_metadata.pdf"
                pdf_jobs.append((metadata_dir, rpt_url, pdf_filename))
            else:
                placeholder_dirs.append(metadata_dir)
        return pdf_jobs, placeholder_dirs

    def run(self):
        """Main entry point — process every job sequentially."""
        succeeded = 0
        failed = 0
        skipped = 0
        failure_log = []  # list of (filename, url, layer, oid, reason)
        total_main = len(self.jobs)

        # Pre-compute metadata PDF jobs so they get counted in the total
        pdf_jobs, placeholder_dirs = self._collect_lidar_metadata_jobs()
        total = total_main + len(pdf_jobs)

        # --- Phase 1: download tiles ---
        for idx, job in enumerate(self.jobs, start=1):
            if self._cancel_requested:
                self.message.emit("Cancel requested — stopping after current progress.")
                break

            filename = job["filename"]
            url = job["url"]
            self.progress.emit(idx, total, filename)

            target = self._target_path(job)

            # Skip if already present and non-empty
            if os.path.exists(target) and os.path.getsize(target) > 0:
                skipped += 1
                continue

            # Try once, then retry once after a short backoff
            ok, err = self._download_one(url, target)
            if not ok:
                time.sleep(1.5)
                ok, err = self._download_one(url, target)

            if ok:
                succeeded += 1
            else:
                failed += 1
                failure_log.append(
                    (
                        filename,
                        url,
                        job.get("layer_name", ""),
                        str(job.get("object_id", "")),
                        err or "unknown error",
                    )
                )

        # --- Phase 2: download metadata PDFs (one per project/scale) ---
        if not self._cancel_requested:
            for j, (metadata_dir, rpt_url, pdf_filename) in enumerate(pdf_jobs, start=1):
                if self._cancel_requested:
                    self.message.emit("Cancel requested — stopping after current progress.")
                    break
                self.progress.emit(total_main + j, total, pdf_filename)
                target = os.path.join(metadata_dir, pdf_filename)
                if os.path.exists(target) and os.path.getsize(target) > 0:
                    skipped += 1
                    continue
                ok, err = self._download_one(rpt_url, target)
                if not ok:
                    time.sleep(1.5)
                    ok, err = self._download_one(rpt_url, target)
                if ok:
                    succeeded += 1
                else:
                    failed += 1
                    failure_log.append(
                        (
                            pdf_filename,
                            rpt_url,
                            "metadata_pdf",
                            "",
                            err or "unknown error",
                        )
                    )

        # --- Phase 3: write placeholder metadata.txt for projects with no PDF ---
        if not self._cancel_requested:
            for metadata_dir in placeholder_dirs:
                self._write_no_metadata_placeholder(metadata_dir)

        # Write failed_downloads.txt if there are any failures
        failed_log_path = ""
        if failure_log:
            failed_log_path = os.path.join(self.output_dir, "failed_downloads.txt")
            try:
                os.makedirs(self.output_dir, exist_ok=True)
                with open(failed_log_path, "w", encoding="utf-8") as fh:
                    fh.write("# LidarBC Batch Downloader — failed downloads\n")
                    fh.write(
                        "# filename\tlayer\tobject_id\turl\treason\n"
                    )
                    for filename, url, layer, oid, reason in failure_log:
                        fh.write(f"{filename}\t{layer}\t{oid}\t{url}\t{reason}\n")
            except OSError as e:
                self.message.emit(f"Could not write failed_downloads.txt: {e}")
                failed_log_path = ""

        self.finished.emit(succeeded, failed, skipped, failed_log_path)
