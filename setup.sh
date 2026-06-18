#!/bin/bash
# Configura o ambiente do Monitor TSE

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "==> Criando ambiente virtual Python..."
python3 -m venv venv
source venv/bin/activate

echo "==> Instalando dependências..."
pip install --quiet --upgrade pip
pip install --quiet requests pdfplumber

echo ""
echo "==> Configurando launchd (agendamento diário às 06:00)..."

# Plist para execução diária do agente
DAILY_PLIST="$HOME/Library/LaunchAgents/com.tse.pesquisas.daily.plist"
cat > "$DAILY_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.tse.pesquisas.daily</string>
    <key>ProgramArguments</key>
    <array>
        <string>$DIR/venv/bin/python3</string>
        <string>$DIR/agent.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>6</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>$DIR/logs/agent.log</string>
    <key>StandardErrorPath</key>
    <string>$DIR/logs/agent.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
PLIST

# Plist para o servidor HTTP (inicia com o login)
SERVER_PLIST="$HOME/Library/LaunchAgents/com.tse.pesquisas.server.plist"
cat > "$SERVER_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.tse.pesquisas.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>$DIR/venv/bin/python3</string>
        <string>$DIR/server.py</string>
    </array>
    <key>StandardOutPath</key>
    <string>$DIR/logs/server.log</string>
    <key>StandardErrorPath</key>
    <string>$DIR/logs/server.log</string>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
PLIST

mkdir -p "$DIR/logs"

# Carrega os agentes launchd
launchctl unload "$DAILY_PLIST" 2>/dev/null || true
launchctl load "$DAILY_PLIST"
launchctl unload "$SERVER_PLIST" 2>/dev/null || true
launchctl load "$SERVER_PLIST"

echo ""
echo "==> Executando o agente pela primeira vez..."
"$DIR/venv/bin/python3" "$DIR/agent.py"

echo ""
echo "============================================================"
echo "  Configuração concluída!"
echo ""
echo "  Relatório disponível em: http://localhost:8766"
echo "  Atualiza automaticamente todo dia às 06:00"
echo ""
echo "  Logs: $DIR/logs/agent.log"
echo "  Para parar o servidor:"
echo "    launchctl unload $SERVER_PLIST"
echo "============================================================"
