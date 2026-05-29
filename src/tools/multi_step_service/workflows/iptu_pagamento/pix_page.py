"""
Página temporária para pagamento Pix de IPTU.
"""

import base64
import html
import json
from typing import Optional


def normalize_base64_image(base64_content: str) -> str:
    if "," in base64_content and base64_content.lstrip().startswith("data:"):
        return base64_content.split(",", 1)[1]
    return base64_content


def image_content_type(file_data: bytes) -> str:
    if file_data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if file_data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if file_data.startswith(b"GIF87a") or file_data.startswith(b"GIF89a"):
        return "image/gif"
    if file_data.startswith(b"RIFF") and file_data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def build_pix_copy_page(qr_code_pix: str, pix_code: Optional[str]) -> str:
    image_base64 = normalize_base64_image(qr_code_pix)
    image_type = image_content_type(base64.b64decode(image_base64))
    escaped_pix_code = html.escape(pix_code or "")
    json_pix_code = json.dumps(pix_code or "")

    return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-store">
  <title>Pix IPTU</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #f4f7fb;
      color: #1f2933;
      font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      width: min(560px, calc(100% - 32px));
      background: #fff;
      border: 1px solid #d8e0ea;
      border-radius: 8px;
      padding: 22px;
      box-shadow: 0 18px 42px rgba(31, 41, 51, 0.08);
    }}
    h1 {{
      margin: 0 0 16px;
      font-size: 1.35rem;
    }}
    img {{
      display: block;
      width: min(260px, 100%);
      height: auto;
      margin: 0 auto 18px;
    }}
    textarea {{
      width: 100%;
      min-height: 120px;
      box-sizing: border-box;
      resize: vertical;
      border: 1px solid #c6d0dd;
      border-radius: 6px;
      padding: 10px;
      font: 0.92rem ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}
    button {{
      width: 100%;
      min-height: 44px;
      margin-top: 12px;
      border: 0;
      border-radius: 6px;
      background: #0f6b5b;
      color: #fff;
      font: inherit;
      font-weight: 750;
      cursor: pointer;
    }}
    #status {{
      min-height: 20px;
      margin-top: 10px;
      color: #526173;
      font-size: 0.9rem;
    }}
  </style>
</head>
<body>
  <main>
    <h1>Pix IPTU</h1>
    <img alt="QR Code Pix" src="data:{image_type};base64,{image_base64}">
    <textarea id="pix" readonly>{escaped_pix_code}</textarea>
    <button type="button" onclick="copyPix()">Copiar código Pix</button>
    <div id="status"></div>
  </main>
  <script>
    async function copyPix() {{
      const pix = {json_pix_code};
      const status = document.querySelector("#status");
      try {{
        await navigator.clipboard.writeText(pix);
        status.textContent = "Código Pix copiado.";
      }} catch (error) {{
        document.querySelector("#pix").select();
        status.textContent = "Selecione e copie o código Pix.";
      }}
    }}
  </script>
</body>
</html>"""


def build_expired_pix_page() -> str:
    return """<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-store">
  <title>Pix IPTU expirado</title>
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #f4f7fb;
      color: #1f2933;
      font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main {
      width: min(520px, calc(100% - 32px));
      background: #fff;
      border: 1px solid #d8e0ea;
      border-radius: 8px;
      padding: 22px;
      box-shadow: 0 18px 42px rgba(31, 41, 51, 0.08);
      text-align: center;
    }
    h1 {
      margin: 0 0 10px;
      font-size: 1.35rem;
    }
    p {
      margin: 0;
      color: #526173;
      line-height: 1.5;
    }
  </style>
</head>
<body>
  <main>
    <h1>Link expirado</h1>
    <p>Este link de pagamento Pix expirou. Solicite uma nova emissão para continuar.</p>
  </main>
</body>
</html>"""
