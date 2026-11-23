#!/usr/bin/env bash
set -euo pipefail

# Levanta la demo con candado ejecutivo neutral (PIN 2026 por defecto)
# listo para exponer temporalmente por Cloudflare Tunnel o ngrok durante sesiones en vivo.

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

export DEMO_ACCESS_PIN="${DEMO_ACCESS_PIN:-2026}"
echo "🔐 Candado Ejecutivo activado: DEMO_ACCESS_PIN=${DEMO_ACCESS_PIN}"
echo "🌐 Para generar el enlace HTTPS público en otra terminal ejecuta:"
echo "   cloudflared tunnel --url http://127.0.0.1:8090"
echo "   (o bien: ngrok http 8090)"
exec "$PROJECT_DIR/scripts/start_local_demo.sh" "$@"
