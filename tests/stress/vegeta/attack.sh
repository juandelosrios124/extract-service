#!/bin/sh
# Carga fija con Vegeta (modelo abierto): envía RATE solicitudes por segundo sin esperar
# las respuestas, rotando los PDFs. Por defecto 50 req/s durante 30s (1500 solicitudes)
# con timeout de cliente de 30s.
#
# Requiere vegeta y jq. Uso y variables de entorno: ver tests/stress/README.md

set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

BASE_URL="${BASE_URL:-http://localhost:8001}"
RATE="${RATE:-50}"
DURATION="${DURATION:-30s}"
TIMEOUT="${TIMEOUT:-30s}"
PDF_DIR="${PDF_DIR:-$SCRIPT_DIR/../pdfs}"
PDF_FILES="${PDF_FILES:-01.pdf 02.pdf 03.pdf 04.pdf}"
OUT_DIR="${OUT_DIR:-$SCRIPT_DIR/../results}"

BOUNDARY="vegeta-stress-boundary"
targets="$OUT_DIR/vegeta-targets.txt"
results="$OUT_DIR/vegeta-results.bin"

mkdir -p "$OUT_DIR/bodies"
: > "$targets"

# Un target por PDF con el body multipart ya armado (campo "file"). Vegeta recorre
# los targets en orden, así que las solicitudes rotan entre los PDFs.
for name in $(echo "$PDF_FILES" | tr ',' ' '); do
  pdf="$PDF_DIR/$name"
  if [ ! -f "$pdf" ]; then
    echo "Falta $pdf (ver tests/stress/pdfs/README.md)" >&2
    exit 1
  fi

  body="$OUT_DIR/bodies/$name.multipart"
  {
    printf -- '--%s\r\n' "$BOUNDARY"
    printf 'Content-Disposition: form-data; name="file"; filename="%s"\r\n' "$name"
    printf 'Content-Type: application/pdf\r\n\r\n'
    cat "$pdf"
    printf -- '\r\n--%s--\r\n' "$BOUNDARY"
  } > "$body"

  printf 'POST %s/extract\nContent-Type: multipart/form-data; boundary=%s\n@%s\n\n' \
    "$BASE_URL" "$BOUNDARY" "$body" >> "$targets"
done

echo "Vegeta: $RATE req/s durante $DURATION contra $BASE_URL/extract (timeout $TIMEOUT)" >&2
vegeta attack -targets="$targets" -rate="$RATE" -duration="$DURATION" -timeout="$TIMEOUT" > "$results"

vegeta report "$results" | tee "$OUT_DIR/vegeta-report.txt"

# Código 0 = el cliente no recibió respuesta; de esos, los timeouts son los que
# terminaron por el timeout del cliente. El resto son errores de conexión.
timeouts=$(vegeta encode -to json "$results" \
  | jq -s '[.[] | select(.code == 0 and (.error | test("Timeout|deadline exceeded")))] | length')

vegeta report -type=json "$results" | jq -r --argjson timeouts "$timeouts" '
  "",
  "== Carga fija (Vegeta, modelo abierto) ==",
  "Solicitudes:          \(.requests)",
  "Éxito:                \(.success * 100 * 100 | round / 100) %",
  "Código 0 (sin resp.): \(.status_codes["0"] // 0)",
  "  de esos, timeouts:  \($timeouts)",
  "Latencia P50:         \(.latencies["50th"] / 1e6 | round) ms",
  "Latencia P95:         \(.latencies["95th"] / 1e6 | round) ms"
' | tee "$OUT_DIR/vegeta-summary.txt"
