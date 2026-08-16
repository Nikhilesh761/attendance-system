import re

with open("app_web.py", "r", encoding="utf-8") as f:
    content = f.read()

NEW_CSS = """<style>
  body { font-family: 'Courier New', monospace; max-width: 900px; margin: 40px auto; padding: 0 20px; background: #000; color: #fff; }
  h1 { font-size: 22px; letter-spacing: 2px; text-transform: uppercase; }
  h2 { font-size: 18px; margin-top: 30px; letter-spacing: 1px; }
  nav a { margin-right: 16px; text-decoration: none; color: #fff; font-weight: bold; border-bottom: 2px solid transparent; }
  nav a:hover { border-bottom: 2px solid #ff0000; }
  table { border-collapse: collapse; width: 100%; margin-top: 12px; }
  th, td { border: 1px solid #333; padding: 8px 10px; text-align: left; font-size: 14px; color: #fff; }
  th { background: #111; text-transform: uppercase; letter-spacing: 1px; }
  .stats { display: flex; gap: 20px; margin: 16px 0; }
  .stat-box { border: 1px solid #333; border-radius: 0; padding: 12px 20px; background: #0a0a0a; }
  .stat-box .num { font-size: 26px; font-weight: bold; color: #ff0000; }
  .stat-box .label { font-size: 12px; color: #999; text-transform: uppercase; }
  button, input[type=submit] { background: #ff0000; color: #fff; border: none; padding: 10px 16px;
    border-radius: 0; cursor: pointer; font-size: 14px; text-transform: uppercase; letter-spacing: 1px; }
  button.danger { background: #660000; }
  input[type=text] { padding: 8px; width: 250px; margin: 4px 0; background: #111; border: 1px solid #333; color: #fff; }
  .consent-box { background: #111; border: 1px solid #ff0000; padding: 14px; border-radius: 0; margin: 14px 0; color: #fff; }
  #video, #canvas { border-radius: 0; border: 1px solid #333; }
</style>"""

pattern = re.compile(r"<style>.*?</style>", re.DOTALL)
matches = pattern.findall(content)

if len(matches) != 1:
    print(f"WARNING: found {len(matches)} <style> blocks, expected 1. Aborting to be safe.")
else:
    new_content = pattern.sub(NEW_CSS, content)
    with open("app_web.py", "w", encoding="utf-8") as f:
        f.write(new_content)
    print("Style block replaced successfully.")