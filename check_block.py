#!/usr/bin/env python3
"""
Validate a Courtready WordPress Custom HTML block.

Checks the constraints that have actually broken builds, not a generic
linter's opinion. Every rule here corresponds to something that shipped
wrong once.
"""

import re
import subprocess
import sys

PASS, FAIL, WARN = [], [], []


def ok(m):
    PASS.append(m)


def bad(m):
    FAIL.append(m)


def warn(m):
    WARN.append(m)


def block(src, tag):
    m = re.search(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), src, re.S)
    return m.group(1) if m else ""


def check(path):
    src = open(path, encoding="utf-8").read()
    css = block(src, "style")
    js = block(src, "script")

    if not css:
        bad("no <style> block found")
        return
    if not js:
        bad("no <script> block found")
        return

    # -- WordPress mangling -----------------------------------------
    for op, name in (("&&", "&&"), ("||", "||")):
        if op in js:
            n = js.count(op)
            bad("%d occurrence(s) of %s in JS; wpautop encodes these"
                % (n, name))
        else:
            ok("no %s in JS" % name)

    if re.search(r"=>", js):
        bad("arrow function in JS")
    else:
        ok("no arrow functions")

    for kw in ("let ", "const "):
        if re.search(r"(^|[^A-Za-z0-9_.])%s" % kw.strip() + r"\s", js):
            bad("%sused in JS" % kw)
        else:
            ok("no %s" % kw.strip())

    if "`" in js:
        bad("backtick template literal in JS")
    else:
        ok("no template literals")

    for api in ("localStorage", "sessionStorage"):
        if api in js:
            bad("%s used; not permitted" % api)
        else:
            ok("no %s" % api)

    # -- blank lines inside style/script -----------------------------
    # Strip the boundary newlines that sit either side of the block
    # content; the constraint is about blank lines within the CSS or JS,
    # not the newline after the opening tag.
    for name, body in (("style", css.strip("\n")), ("script", js.strip("\n"))):
        blanks = [i + 1 for i, l in enumerate(body.split("\n"))
                  if l.strip() == ""]
        if blanks:
            bad("%d blank line(s) inside <%s> at %s"
                % (len(blanks), name, blanks[:6]))
        else:
            ok("no blank lines inside <%s>" % name)

    # -- style placement --------------------------------------------
    si = src.find("<style")
    di = src.find('<div id=')
    if si == -1 or di == -1 or si > di:
        bad("<style> must come before and outside the root div")
    else:
        ok("<style> precedes the root div")

    root = re.search(r'<div id="([a-z]+-tool-wrapper)"', src)
    if not root:
        bad("no root wrapper div found")
        return
    wrapper = root.group(1)
    ok("root wrapper is #%s" % wrapper)

    body_after = src[di:]
    if "<style" in body_after:
        bad("<style> nested inside the root div; WordPress strips the parent")
    else:
        ok("no <style> nested in the root div")

    # -- file ending -------------------------------------------------
    if src.rstrip().endswith("})();</script>"):
        ok("file ends })();</script>")
    else:
        bad("file must end exactly with })();</script>")

    # -- prefixing ---------------------------------------------------
    prefix = wrapper.split("-")[0]
    ids = set(re.findall(r'id="([A-Za-z][\w-]*)"', src))
    stray = [i for i in ids
             if not i.startswith(prefix) and i != wrapper]
    if stray:
        bad("unprefixed id(s): %s" % ", ".join(sorted(stray)[:8]))
    else:
        ok("every id carries the %s prefix" % prefix)

    # Only scan the markup. The script block contains JS string
    # concatenation like 'class="' + cls + '"' which is not markup.
    markup = re.sub(r"<script[^>]*>.*?</script>", "", src, flags=re.S)
    classes = set(re.findall(r'class="([^"]+)"', markup))
    flat = set()
    for c in classes:
        for one in c.split():
            flat.add(one)
    stray = [c for c in flat if not c.startswith(prefix + "-")]
    if stray:
        bad("unprefixed class(es): %s" % ", ".join(sorted(stray)[:8]))
    else:
        ok("every class carries the %s- prefix" % prefix)

    # -- email obfuscation -------------------------------------------
    # example.com and friends are RFC 2606 reserved for documentation.
    # A placeholder in an input field is not a harvestable address.
    found = [m for m in re.findall(
        r"[A-Za-z0-9._%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", src)
        if not re.search(r"@example\.(com|org|net)$", m)]
    if found:
        bad("a bare email address appears (%s); use 'admin [at] "
            "courtready.ca'" % found[0])
    else:
        ok("no harvestable email address")

    # -- CSS trap 1: specificity against the reset -------------------
    # Reset selector scores (1,0,2). Any rule styling text must include
    # a class and declare font-size, or it silently loses.
    rules = re.findall(r"([^{}]+)\{([^}]*)\}", css)
    text_props = ("color:", "font-weight:", "font-style:", "font-family:",
                  "letter-spacing:", "text-transform:", "line-height:")
    spec_bad = []
    for sel, body in rules:
        sel = sel.strip()
        if sel.startswith("@") or "keyframes" in sel:
            continue
        parts = [s.strip() for s in sel.split(",")]
        for p in parts:
            if wrapper not in p:
                continue
            tail = p.split(wrapper, 1)[1]
            if "." not in tail:
                if any(t in body for t in text_props):
                    if "font-size:" not in body:
                        spec_bad.append(p)
    if spec_bad:
        bad("rule(s) style text, omit font-size, and have no class; "
            "they lose to the reset: %s" % "; ".join(spec_bad[:4]))
    else:
        ok("no rule loses to the reset on specificity")

    # -- CSS trap 2: shorthand after its own longhand ----------------
    pairs = {"margin": "margin-", "padding": "padding-",
             "border": "border-", "background": "background-",
             "font": "font-"}
    # Properties that share a prefix but are NOT part of the shorthand,
    # so the shorthand does not reset them.
    not_longhand = ("border-radius", "border-collapse", "border-spacing",
                    "border-image")
    short_bad = []
    for sel, body in rules:
        decls = [d.strip() for d in body.split(";") if d.strip()]
        seen_long = {}
        for i, d in enumerate(decls):
            prop = d.split(":", 1)[0].strip()
            for short, pre in pairs.items():
                if prop.startswith(pre):
                    if prop not in not_longhand:
                        seen_long.setdefault(short, i)
                if prop == short:
                    if short in seen_long:
                        if seen_long[short] < i:
                            short_bad.append(
                                "%s: %s after %s" % (sel.strip()[:40],
                                                     short, pre + "*"))
    if short_bad:
        bad("shorthand after its own longhand resets it: %s"
            % "; ".join(short_bad[:4]))
    else:
        ok("no shorthand-after-longhand")

    # -- CSS trap 3: flex-basis in a column container ----------------
    for sel, body in rules:
        if "flex-direction: column" in body:
            if re.search(r"flex:\s*\d+\s+\d+\s+\d+px", body):
                warn("%s sets a px flex-basis in a column container"
                     % sel.strip()[:40])

    # -- SVG excluded from the reset ---------------------------------
    if ":not(svg):not(svg *)" in css:
        ok("SVG excluded from the reset")
    else:
        bad("reset does not exclude SVG; icons will break")

    # -- JS parses ---------------------------------------------------
    try:
        proc = subprocess.run(
            ["node", "-e",
             "var s=require('fs').readFileSync(process.argv[1],'utf8');"
             "var m=s.match(/<script>([\\s\\S]*?)<\\/script>/);"
             "new Function(m[1]);console.log('PARSE_OK');",
             path],
            capture_output=True, text=True, timeout=30)
        if "PARSE_OK" in proc.stdout:
            ok("JS parses via new Function")
        else:
            bad("JS failed to parse: %s"
                % (proc.stderr.strip().split("\n")[0] if proc.stderr
                   else "unknown"))
    except Exception as e:
        warn("could not run node to parse JS: %s" % e)

    # -- inline JSON-LD sanity ---------------------------------------
    for m in re.finditer(r'type="application/ld\+json"[^>]*>(.*?)</script>',
                         src, re.S):
        import json
        try:
            json.loads(m.group(1))
            ok("JSON-LD parses")
        except ValueError as e:
            bad("JSON-LD invalid: %s" % e)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "vcd-block.html"
    check(path)
    for m in PASS:
        print("  ok    %s" % m)
    for m in WARN:
        print("  warn  %s" % m)
    for m in FAIL:
        print("  FAIL  %s" % m)
    print()
    print("%d passed, %d warnings, %d failed"
          % (len(PASS), len(WARN), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
