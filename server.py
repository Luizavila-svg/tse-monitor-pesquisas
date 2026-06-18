#!/usr/bin/env python3
"""
Servidor HTTP para o relatório TSE.
Serve a pasta reports/ na porta 8766.
"""

import http.server
import os
from pathlib import Path

PORT = 8766
REPORTS_DIR = Path(__file__).parent / "reports"

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(REPORTS_DIR), **kwargs)

    def log_message(self, fmt, *args):
        pass  # silencia logs de acesso

if __name__ == "__main__":
    os.chdir(REPORTS_DIR)
    server = http.server.HTTPServer(("", PORT), Handler)
    print(f"Servidor TSE rodando em http://localhost:{PORT}")
    print("Pressione Ctrl+C para parar.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
