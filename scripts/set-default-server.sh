#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

NEW_HOST=""
NEW_PORT=""
NEW_WEBSITE=""
NEW_VERSION_ORIGIN=""
NEW_DOWNLOAD_ORIGIN=""
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: ./scripts/set-default-server.sh --host <DOMAIN_OR_IP> [options]

Updates hardcoded CrossDesk official server endpoints in client/ to your own server.

Options:
  --host <HOST>                  New default signaling server host (replaces api.crossdesk.cn)
  --port <PORT>                  Optional new default signaling port (replaces 9099 in client defaults)
  --website <URL>                Optional website URL (replaces https://crossdesk.cn and https://www.crossdesk.cn/)
  --version-origin <URL>         Optional version manifest origin (replaces https://version.crossdesk.cn)
  --download-origin <URL>        Optional download origin (replaces https://downloads.crossdesk.cn)
  --dry-run                      Preview files that would change without modifying them
  -h, --help                     Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) NEW_HOST="$2"; shift 2 ;;
    --port) NEW_PORT="$2"; shift 2 ;;
    --website) NEW_WEBSITE="$2"; shift 2 ;;
    --version-origin) NEW_VERSION_ORIGIN="$2"; shift 2 ;;
    --download-origin) NEW_DOWNLOAD_ORIGIN="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -z "$NEW_HOST" ]]; then
  echo "Error: --host is required." >&2
  usage
  exit 1
fi

python3 - "$ROOT_DIR" "$NEW_HOST" "$NEW_PORT" "$NEW_WEBSITE" "$NEW_VERSION_ORIGIN" "$NEW_DOWNLOAD_ORIGIN" "$DRY_RUN" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
new_host = sys.argv[2]
new_port = sys.argv[3]
new_website = sys.argv[4].rstrip("/")
new_version = sys.argv[5].rstrip("/")
new_download = sys.argv[6].rstrip("/")
dry_run = sys.argv[7] == "1"

host_files = [
    "client/apps/desktop/src/config_center/config_center.h",
    "client/apps/desktop/src/gui/runtime/runtime_state.h",
    "client/libs/controller/include/controller_session.h",
    "client/apps/android/app/src/main/java/cn/crossdesk/mobile/MainActivity.java",
    "client/apps/ios/CrossDeskMobile/RemoteSessionModel.swift",
    "client/apps/harmony/entry/src/main/ets/pages/Mobile.ets",
    "client/apps/harmony/entry/src/main/ets/mobile/MobileController.ets",
    "client/apps/mcp/src/store.ts",
    "client/apps/mcp/ui/viewer.ts",
]

website_files = [
    "client/apps/desktop/src/gui/application/gui_application.cpp",
    "client/apps/desktop/src/gui/ui/about_panel.slint",
    "client/apps/desktop/src/gui/ui/main_window.slint",
    "client/apps/desktop/src/updater/update_core.cpp",
    "client/apps/desktop/src/version_checker/version_checker.cpp",
    "client/apps/desktop/scripts/windows/nsis_script.nsi",
    "client/apps/android/app/src/main/java/cn/crossdesk/mobile/AboutPages.java",
    "client/apps/harmony/entry/src/main/ets/pages/Mobile.ets",
]

version_files = [
    "client/apps/desktop/src/version_checker/version_checker.cpp",
    "client/apps/android/app/src/main/java/cn/crossdesk/mobile/RemoteVersionCheck.java",
    "client/apps/ios/CrossDeskMobile/RemoteSessionModel.swift",
    "client/apps/harmony/entry/src/main/ets/mobile/MobilePlatform.ets",
]

download_files = [
    "client/apps/desktop/src/updater/update_core.cpp",
    "client/apps/desktop/src/version_checker/linux_update_download.cpp",
]

all_targets = sorted(set(host_files + website_files + version_files + download_files))
changed = []

for rel in all_targets:
    path = root / rel
    if not path.exists():
        continue
    original = path.read_text(encoding="utf-8")
    updated = original

    if rel in host_files:
        updated = updated.replace("api.crossdesk.cn", new_host)
        if new_port:
            updated = updated.replace("9099", new_port)

    if new_website and rel in website_files:
        updated = updated.replace("https://www.crossdesk.cn/", f"{new_website}/")
        updated = updated.replace("https://crossdesk.cn", new_website)

    if new_version and rel in version_files:
        updated = updated.replace("https://version.crossdesk.cn", new_version)

    if new_download and rel in download_files:
        updated = updated.replace("https://downloads.crossdesk.cn", new_download)

    if updated != original:
        changed.append(rel)
        if not dry_run:
            path.write_text(updated, encoding="utf-8")

mode = "[DRY RUN] Would update" if dry_run else "Updated"
print(f"{mode} {len(changed)} files:")
for f in changed:
    print(f"  - {f}")
PY
