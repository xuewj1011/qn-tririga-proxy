import os

secret_val = "WmePy3zAqfzm86SbXf2lJfCPRlBLiQEE"
env_path = r"C:\Users\xuewj\AppData\Local\hermes\.env"

with open(env_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

new_lines = []
found = False
for line in lines:
    if line.startswith("FEISHU_APP_SECRET="):
        new_lines.append(f"FEISHU_APP_SECRET={secret_val}\n")
        found = True
    else:
        new_lines.append(line)

if not found:
    new_lines.append(f"FEISHU_APP_SECRET={secret_val}\n")

with open(env_path, "w", encoding="utf-8") as f:
    f.writelines(new_lines)

print("Wrote full secret. Verifying:")
with open(env_path, "r") as f:
    for line in f:
        if "FEISHU_APP_SECRET" in line:
            val = line.strip().split("=", 1)[1]
            print(f"Length: {len(val)}, starts with: {val[:8]}, ends with: {val[-4:]}")
