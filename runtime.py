"""Shared, mutable runtime knobs the owner tunes live via /settings.

A plain module global can't be shared across modules once its readers and
its writer live in different files: ``global X`` only rebinds the *defining*
module's copy, so a setter in settings.py would silently leave bot.py's loops
reading a stale value. These knobs therefore live on one shared object
(``rt``) that every reader imports, so a change is seen everywhere at once.
Each tunable is also persisted to .env by the settings menu so it survives a
restart. The test harness re-imports the module per test, so each test gets a
fresh ``Runtime`` seeded from that test's patched env; tests redirect a knob
by setting the attribute on the shared ``rt`` object.
"""
import os


class Runtime:
    def __init__(self):
        # Where downloaded media and extracted frames are staged. Not tuned at
        # runtime, but kept here so every reader shares one patchable value.
        self.upload_dir = "/tmp/bot_uploads"
        self.upload_ttl_s = int(os.environ.get("UPLOAD_TTL_S", str(48 * 3600)))
        self.upload_warn_bytes = (
            int(os.environ.get("UPLOAD_WARN_MB", "500")) * 1024 * 1024)
        self.default_display = os.environ.get("DEFAULT_DISPLAY", "mobile")
        self.auto_update = os.environ.get("AUTO_UPDATE", "false").lower() in (
            "true", "1", "yes")
        self.auto_update_policy = os.environ.get(
            "AUTO_UPDATE_POLICY", "replace")
        self.temp_note = self._build_temp_note()

    def _build_temp_note(self) -> str:
        """The retention note appended to every turn that hands Claude an
        upload path. Re-derived when /settings changes the TTL so the stated
        retention is always accurate."""
        return (f"(note: attached files under {self.upload_dir} are temporary "
                f"— auto-deleted after ~{self.upload_ttl_s // 3600}h; copy "
                f"anything needed long-term into the project)")

    def set_upload_ttl_s(self, seconds: int) -> None:
        """Change the upload TTL and re-derive the dependent temp note."""
        self.upload_ttl_s = seconds
        self.temp_note = self._build_temp_note()


# Process-wide singleton, shared by reference across every importer.
rt = Runtime()
