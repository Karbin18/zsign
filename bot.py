"""
ZSign Telegram Bot — Production Grade
Supports: IPA signing via zsign + Netlify static hosting
"""

import os
import json
import uuid
import shlex
import shutil
import asyncio
import logging
import plistlib
import urllib.parse
from pathlib import Path
from zipfile import ZipFile, BadZipFile

from telegram import Update, constants
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("zsign-bot")

# ──────────────────────────────────────────────
# Config (loaded once at startup)
# ──────────────────────────────────────────────
class Config:
    """Validates and holds runtime configuration."""

    REQUIRED = ["token", "p12", "password", "mobileprovision", "domain"]

    def __init__(self, path: str = "config.json"):
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        missing = [k for k in self.REQUIRED if not raw.get(k)]
        if missing:
            raise ValueError(f"config.json is missing required keys: {missing}")

        self.token: str = raw["token"]
        self.p12: str = raw["p12"]
        self.password: str = raw["password"]
        self.mobileprovision: str = raw["mobileprovision"]
        self.domain: str = raw["domain"].rstrip("/")

        # Optional zsign flags
        self.zsign_extra: str = raw.get("zsign_extra", "-z 9")  # max compression

        # File size limit (bytes). Default 500 MB.
        self.max_size: int = raw.get("max_size_mb", 500) * 1024 * 1024

        # Directory layout (Netlify serves from ./public/)
        self.public_dir = Path(raw.get("public_dir", "public"))
        self.signed_dir = self.public_dir / "signed"
        self.plist_dir  = self.public_dir / "plist"

        for d in (self.signed_dir, self.plist_dir):
            d.mkdir(parents=True, exist_ok=True)

        log.info("Config loaded. Domain: %s", self.domain)


# ──────────────────────────────────────────────
# IPA metadata extraction
# ──────────────────────────────────────────────
def extract_ipa_metadata(ipa_path: Path) -> dict | None:
    """
    Reads Info.plist from the IPA zip and returns bundle metadata.
    Returns None on any failure so the caller can handle gracefully.
    """
    try:
        with ZipFile(ipa_path, "r") as zf:
            plists = [
                n for n in zf.namelist()
                if n.endswith(".app/Info.plist") and "__MACOSX" not in n
            ]
            if not plists:
                log.warning("No Info.plist found in %s", ipa_path)
                return None

            # Pick the shortest path (most likely the root app bundle)
            plists.sort(key=len)
            with zf.open(plists[0]) as f:
                data = plistlib.load(f)

            bundle_id = data.get("CFBundleIdentifier")
            if not bundle_id:
                log.warning("CFBundleIdentifier missing in %s", ipa_path)
                return None

            return {
                "bundle_id": bundle_id,
                "version":   data.get("CFBundleShortVersionString", "1.0"),
                "name":      data.get("CFBundleDisplayName")
                             or data.get("CFBundleName", "App"),
            }
    except BadZipFile:
        log.error("Not a valid zip/IPA: %s", ipa_path)
        return None
    except Exception as exc:
        log.exception("Unexpected error reading IPA metadata: %s", exc)
        return None


# ──────────────────────────────────────────────
# zsign runner
# ──────────────────────────────────────────────
async def run_zsign(cfg: Config, input_path: Path, output_path: Path) -> tuple[bool, str]:
    """
    Invokes zsign asynchronously.
    Returns (success: bool, stderr_output: str).
    """
    # shlex.quote prevents any path injection
    cmd = (
        f"zsign "
        f"-k {shlex.quote(cfg.p12)} "
        f"-p {shlex.quote(cfg.password)} "
        f"-m {shlex.quote(cfg.mobileprovision)} "
        f"-o {shlex.quote(str(output_path))} "
        f"{cfg.zsign_extra} "
        f"{shlex.quote(str(input_path))}"
    )
    log.info("Running: %s", cmd)

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    success = proc.returncode == 0
    if not success:
        log.error("zsign failed (rc=%d): %s", proc.returncode, stderr.decode())
    else:
        log.info("zsign succeeded: %s", output_path.name)

    return success, stderr.decode(errors="replace")


# ──────────────────────────────────────────────
# Plist generator
# ──────────────────────────────────────────────
def write_manifest_plist(path: Path, ipa_url: str, meta: dict) -> None:
    """
    Writes a proper Apple OTA manifest plist (XML, not binary)
    so it works reliably with itms-services://.
    """
    manifest = {
        "items": [
            {
                "assets": [
                    {"kind": "software-package", "url": ipa_url}
                ],
                "metadata": {
                    "bundle-identifier": meta["bundle_id"],
                    "bundle-version":    meta["version"],
                    "kind":              "software",
                    "title":             meta["name"],
                },
            }
        ]
    }
    # fmt=FMT_XML ensures human-readable and maximum compatibility
    with open(path, "wb") as f:
        plistlib.dump(manifest, f, fmt=plistlib.FMT_XML)


# ──────────────────────────────────────────────
# Install link builder
# ──────────────────────────────────────────────
def build_install_url(manifest_url: str) -> str:
    """
    Correctly encodes the manifest URL as an itms-services query param.
    The *entire* manifest_url must be percent-encoded as a value.
    """
    encoded = urllib.parse.quote(manifest_url, safe="")
    return f"itms-services://?action=download-manifest&url={encoded}"

def write_plist(self, job_id: str, meta: dict):
    manifest = {
        "items": [{
            "assets": [
                {
                    "kind": "software-package",
                    "url": self._ipa_url(job_id)
                },
                {
                    # Required placeholder — iOS won't install without it
                    "kind": "display-image",
                    "url": f"{self.cfg['domain']}/icon.png"
                },
                {
                    "kind": "full-size-image",
                    "url": f"{self.cfg['domain']}/icon.png"
                },
            ],
            "metadata": {
                "bundle-identifier": meta["bundle_id"],
                "bundle-version":    meta["version"],
                "kind":              "software",
                "title":             meta["name"],
            },
        }]
    }
    out = self.dirs.plist / f"{job_id}.plist"
    with open(out, "wb") as f:
        plistlib.dump(manifest, f)
    return out

# ──────────────────────────────────────────────
# Telegram handlers
# ──────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *ZSign Bot*\n\n"
        "Send me an `.ipa` file and I'll sign it for you instantly.\n\n"
        "📋 Supported: Any IPA up to 500 MB.",
        parse_mode=constants.ParseMode.MARKDOWN,
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.bot_data["cfg"]
    doc = update.message.document

    # ── Guard: must be an IPA ──
    if not doc or not (doc.file_name or "").lower().endswith(".ipa"):
        await update.message.reply_text("⚠️ Please send an `.ipa` file.")
        return

    # ── Guard: file size ──
    if doc.file_size and doc.file_size > cfg.max_size:
        limit_mb = cfg.max_size // (1024 * 1024)
        await update.message.reply_text(f"❌ File too large. Limit is {limit_mb} MB.")
        return

    # Use a UUID-based working directory to avoid filename collisions
    job_id   = uuid.uuid4().hex[:12]
    work_dir = Path("tmp") / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    # Sanitise original filename (keep extension)
    safe_stem = Path(doc.file_name).stem.replace(" ", "_")
    ipa_in    = work_dir / f"{safe_stem}.ipa"

    status = await update.message.reply_text(
        "⏳ *Step 1 / 3 — Downloading…*",
        parse_mode=constants.ParseMode.MARKDOWN,
    )

    try:
        # ── Step 1: Download ──
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(str(ipa_in))
        log.info("[%s] Downloaded %s (%.1f MB)", job_id, doc.file_name, ipa_in.stat().st_size / 1e6)

        # ── Step 2: Extract metadata ──
        meta = extract_ipa_metadata(ipa_in)
        if not meta:
            await status.edit_text("❌ Could not read IPA metadata. Is this a valid IPA?")
            return

        await status.edit_text(
            "🔐 *Step 2 / 3 — Signing…*",
            parse_mode=constants.ParseMode.MARKDOWN,
        )

        # ── Step 3: Sign ──
        signed_name = f"{safe_stem}_{job_id}.ipa"
        ipa_out     = cfg.signed_dir / signed_name
        success, err = await run_zsign(cfg, ipa_in, ipa_out)

        if not success:
            await status.edit_text(
                f"❌ Signing failed.\n\n```\n{err[:800]}\n```",
                parse_mode=constants.ParseMode.MARKDOWN,
            )
            return

        await status.edit_text(
            "📝 *Step 3 / 3 — Generating install link…*",
            parse_mode=constants.ParseMode.MARKDOWN,
        )

        # ── Step 4: Generate plist & links ──
        plist_name   = f"{safe_stem}_{job_id}.plist"
        plist_path   = cfg.plist_dir / plist_name

        ipa_url      = f"{cfg.domain}/signed/{signed_name}"
        manifest_url = f"{cfg.domain}/plist/{plist_name}"
        install_url  = build_install_url(manifest_url)

        write_manifest_plist(plist_path, ipa_url, meta)
        log.info("[%s] Plist written: %s", job_id, plist_path)

        # ── Reply ──
        await status.delete()

        # Message 1: result summary + clickable button
        await update.message.reply_text(
            f"✅ *{meta['name']} — Signed Successfully\\!*\n\n"
            f"📦 Bundle ID: `{meta['bundle_id']}`\n"
            f"🔢 Version:   `{meta['version']}`\n\n"
            f"📲 *How to install:*\n"
            f"1\\. Copy the link below\n"
            f"2\\. Open Safari on your iPhone\n"
            f"3\\. Paste the link and go\n"
            f"4\\. Trust the cert: Settings → General → VPN & Device Management",
            parse_mode=constants.ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )

        # Message 2: raw install link — easy to copy on any device
        await update.message.reply_text(
            f"`{install_url}`",
            parse_mode=constants.ParseMode.MARKDOWN_V2,
        )

    except Exception as exc:
        log.exception("[%s] Unhandled error: %s", job_id, exc)
        await status.edit_text("💥 An unexpected error occurred. Please try again.")
    finally:
        # Always clean up the temp working directory
        shutil.rmtree(work_dir, ignore_errors=True)


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────
def main() -> None:
    cfg = Config()

    app = (
        ApplicationBuilder()
        .token(cfg.token)
        .concurrent_updates(True)   # handle multiple users simultaneously
        .build()
    )

    app.bot_data["cfg"] = cfg

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    log.info("Bot started. Polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
