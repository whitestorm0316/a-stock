#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抽出 HTML 里的内联 <script> 体存成 .js, 供 node --check 校验"""
import re
import sys
import os

src = sys.argv[1]
dst = sys.argv[2]

html = open(src, encoding="utf-8").read()
# 只取没有 src 属性的内联 script
blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, flags=re.S)
print(f"inline script blocks: {len(blocks)}")
body = "\n;\n".join(blocks)
open(dst, "w", encoding="utf-8").write(body)
print(f"wrote {dst}  {len(body)/1024:.1f} KB")
