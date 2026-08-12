#!/usr/bin/env bash
# collect-monitor-info.sh — READ-ONLY DDC/CI info collector for Dell monitors.
#
# Gathers as much as possible about every directly-attached monitor so a new Dell
# model (or a not-yet-working feature) can be added to plasma-dell-monitor-support.
# It performs NO writes — it cannot change any monitor setting, so it is safe to
# run and safe to share the resulting report.
#
# For each monitor it collects:
#   * EDID identity (manufacturer / model / serial / VCP version) + whether it's Dell
#   * the raw capability string AND ddcutil's parsed capabilities
#   * the list of every VCP code the monitor ADVERTISES (from the capabilities)
#   * a getvcp probe of (advertised codes) ∪ (a curated superset of known-interesting
#     standard + 0xE0-0xFF manufacturer codes), each with a per-code timeout so a
#     slow/quirky panel can't hang the run
#   * for every code: its ddcutil vcpinfo attributes (Read/Write/Read-Only/Write-Only)
#   * write-only codes are listed explicitly even though they can't be read
#
# With --full it probes the ENTIRE 0x00-0xFF range instead of the curated set
# (nothing is hidden — but it is slow; see the warning below).
#
# Usage:
#   bash collect-monitor-info.sh            # advertised + curated codes (recommended)
#   bash collect-monitor-info.sh --full     # every code 0x00-0xFF (exhaustive, slow)
#
# Output is printed and saved to a timestamped text file — send that file back to
# add support for a new model.
set -u

FULL=0
for arg in "$@"; do
  case "$arg" in
    --full) FULL=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)"; exit 2 ;;
  esac
done

# Per-code read timeout (seconds). Bounds the worst case on slow / MST panels so a
# single unlucky code can't stall the whole run.
TMO="${DDC_PROBE_TIMEOUT:-8}"

OUT="monitor-info-$(date +%Y%m%d-%H%M%S).txt"
exec > >(tee "$OUT") 2>&1

# Curated READ set: broad standard MCCS codes we care about (incl. the odd ones
# seen on real Dells: 0E/1E/1F/20/30/3E, and DC/AA/B2 that the ASUS-era list
# missed) + the full 0xE0-0xFF manufacturer range where Dell's extras live.
CURATED_STD="02 04 05 06 08 0B 0C 0E 10 12 14 16 18 1A 1E 1F 20 30 3E 52 59 5A 5B 5C 5D 5E 5F 60 62 63 6C 6E 70 72 86 87 8A 8D 90 9B 9C 9D 9E 9F A0 AA AC AE B0 B2 B6 C0 C6 C8 C9 CA CC CE D0 D6 DA DC DF"
CURATED_MFR="E0 E1 E2 E3 E4 E5 E6 E7 E8 E9 EA EB EC ED EE EF F0 F1 F2 F3 F4 F5 F6 F7 F8 F9 FA FB FC FD FE FF"

echo "=================================================================="
echo " plasma-dell-monitor-support — monitor info collector (READ-ONLY)"
echo "=================================================================="
if [ "$FULL" = 1 ]; then
  echo "MODE: --full  (probing ALL 256 codes 0x00-0xFF per monitor)"
else
  echo "MODE: standard (advertised codes + curated known-interesting set)"
fi
echo
echo "⚠  This may take a WHILE — often 1-2 minutes, and MORE THAN 2 MINUTES"
echo "   with --full or on an MST daisy-chain (ddcutil is slow to probe those)."
echo "   It only READS from your monitors — no setting is ever changed."
echo "   Per-code timeout: ${TMO}s.  Please be patient; do not interrupt."
echo

echo "=== ddcutil version ==="
ddcutil --version 2>&1 | head -1
echo
echo "=== ddcutil detect ==="
# `ddcutil detect` does NOT accept -b, so capture it once and slice per bus below.
DETECT_ALL=$(ddcutil detect 2>&1)
echo "$DETECT_ALL"

# Buses of valid (non-invalid, non-laptop) displays.
buses=$(ddcutil detect --terse 2>/dev/null | awk '
  /^Display/{ok=1} /^Invalid/{ok=0}
  ok && /I2C bus:/{ sub(/.*i2c-/,""); print }')

if [ -z "$buses" ]; then
  echo
  echo "No controllable displays detected. Enable DDC/CI in the monitor OSD and"
  echo "check i2c-dev access (see README/INSTALL), then re-run."
  echo
  echo "=== DONE — report saved to: $OUT ==="
  exit 0
fi

# vcpinfo attributes for a code (static metadata; takes NO -b). Cached per run.
declare -A VCPINFO_CACHE
vcp_attrs() {
  local c="$1"
  if [ -z "${VCPINFO_CACHE[$c]+x}" ]; then
    VCPINFO_CACHE[$c]=$(ddcutil vcpinfo "$c" 2>/dev/null \
      | grep -iE 'Attributes' | tail -1 | sed 's/^ *//')
  fi
  printf '%s' "${VCPINFO_CACHE[$c]}"
}

for B in $buses; do
  echo
  echo "########################  BUS $B  ########################"

  echo "--- identity ---"
  # Slice this bus's block out of the full detect capture (detect has no -b).
  block=$(printf '%s\n' "$DETECT_ALL" | awk -v re="i2c-$B([^0-9]|\$)" '
    /^Display|^Invalid display/ { if (hit && !done) {printf "%s", buf; done=1} buf=""; hit=0 }
    !done { buf = buf $0 "\n" }
    $0 ~ re { hit=1 }
    END { if (hit && !done) printf "%s", buf }')
  ident=$(printf '%s\n' "$block" | grep -iE 'Mfg id|Model:|Serial number|VCP version|DRM' | sed 's/^ *//')
  echo "$ident"
  if printf '%s\n' "$block" | grep -qiE 'Mfg id:[[:space:]]*DEL'; then
    echo "Dell: YES"
  else
    echo "Dell: no (this tool targets Dell; other vendors are collected too, FYI)"
  fi
  echo

  echo "--- raw capability string ---"
  caps_raw=$(ddcutil -b "$B" capabilities --terse 2>&1)
  echo "$caps_raw" | grep -i "Unparsed capabilities" || echo "(no capability string returned on this input)"
  echo
  echo "--- full parsed capabilities (ddcutil's interpretation) ---"
  caps_parsed=$(ddcutil -b "$B" capabilities 2>&1)
  echo "$caps_parsed"
  echo

  # Codes the monitor ADVERTISES, from the parsed capabilities ("Feature: NN").
  advertised=$(echo "$caps_parsed" | grep -oiE 'Feature: [0-9a-f]{2}' \
    | awk '{print toupper($2)}' | sort -u)
  echo "--- advertised VCP codes ($(echo "$advertised" | grep -c .)) ---"
  echo "$advertised" | paste -sd' ' -
  echo

  # Build the probe set.
  if [ "$FULL" = 1 ]; then
    codes=$(for i in $(seq 0 255); do printf '%02X\n' "$i"; done)
  else
    codes=$(printf '%s\n' $CURATED_STD $CURATED_MFR $advertised \
      | tr 'a-f' 'A-F' | sort -u)
  fi

  echo "--- READ-ONLY code probe ---"
  echo "    format: 0xNN | <vcpinfo attributes> | <getvcp result / note>"
  is_adv() { echo "$advertised" | grep -qx "$1"; }
  answered=0; wo=0; adv_noread=0
  while read -r c; do
    [ -z "$c" ] && continue
    val=$(timeout "$TMO" ddcutil -b "$B" getvcp "0x$c" --terse 2>&1)
    rc=$?
    attrs=$(vcp_attrs "$c"); attrs="${attrs:-(manufacturer-specific, no metadata)}"
    if [ "$rc" -eq 124 ]; then
      # timed out — only worth reporting if the monitor advertises it
      is_adv "$c" && printf "0x%s | %-54s | (TIMED OUT after %ss)\n" "$c" "$attrs" "$TMO"
      continue
    fi
    case "$val" in
      VCP*)
        printf "0x%s | %-54s | %s\n" "$c" "$attrs" "$val"; answered=$((answered+1)) ;;
      *)
        # No read value. Surface it anyway if it's advertised (likely write-only
        # or a table type) or if vcpinfo marks it Write Only.
        if echo "$attrs" | grep -qi 'Write Only'; then
          printf "0x%s | %-54s | (write-only — cannot be read)\n" "$c" "$attrs"; wo=$((wo+1))
        elif is_adv "$c"; then
          printf "0x%s | %-54s | (advertised but no read response — write-only/table?)\n" "$c" "$attrs"
          adv_noread=$((adv_noread+1))
        fi
        ;;
    esac
  done <<< "$codes"
  echo
  echo "    summary: ${answered} readable, ${wo} write-only, ${adv_noread} advertised-but-unread"
done

echo
echo "=================================================================="
echo " DONE (read-only; NO settings were changed)."
echo " Report saved to: $OUT"
echo " Send this file to add support for your monitor."
echo "=================================================================="
