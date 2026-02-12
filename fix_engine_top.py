"""Fix the broken top of engine.py"""
import re

with open('src/engine.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Find the last 'import asyncio' that's near the real imports
matches = list(re.finditer(r'import asyncio', content))
print(f"Found {len(matches)} 'import asyncio' occurrences")
for i, m in enumerate(matches):
    ctx = content[m.start():m.start()+80].replace('\n', '\\n')
    print(f"  [{i}] pos={m.start()}: {ctx}")

# Take the LAST one (that's the real import block)
if matches:
    idx = matches[-1].start()
    docstring = '"""SHF Trading Engine v5.6.3 - Oil + Index Duo"""\n\n'
    clean = docstring + content[idx:]
    with open('src/engine.py', 'w', encoding='utf-8') as f:
        f.write(clean)
    print(f"\nFIXED! Wrote {len(clean)} chars")
    print("First 3 lines:")
    for i, line in enumerate(clean.split('\n')[:3]):
        print(f"  {i+1}: {line}")
