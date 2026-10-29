"""
Diagnostic probe: why does bccourts.ca refuse or ignore GitHub runners?

Runs ONLY on the diag/court-probe branch. Never touches data/, never
publishes, never sends alerts. Writes one plain-text report to stdout.

Questions it answers:
  1. Which addresses does www.bccourts.ca resolve to, per resolver?
  2. Does every address accept connections, or is one of them broken?
  3. Does a burst of requests (the way the scraper fetches) start being
     refused or stalled part way through, which would mean a limiter?
  4. Does the court treat our bot User-Agent differently from a browser?
"""

import os
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

HOST = "www.bccourts.ca"
INDEX_PATH = "/supreme_court/scheduling/index.aspx"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

T0 = time.time()


def out(msg=""):
    print("[%6.1fs] %s" % (time.time() - T0, msg), flush=True)


def sh(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return "(timed out after %ss)" % timeout


def tcp(ip, port=443, timeout=10):
    t = time.time()
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.close()
        return "ok %4.0fms" % ((time.time() - t) * 1000)
    except ConnectionRefusedError:
        return "REFUSED %4.0fms" % ((time.time() - t) * 1000)
    except socket.timeout:
        return "TIMEOUT %.0fs" % timeout
    except OSError as e:
        return "ERR %s" % e


def raw_get(ip, path, ua, timeout=30, head_only=False):
    """One HTTPS request to a specific IP, SNI and Host set to HOST.
    Certificate is not verified: this measures reachability, not trust."""
    t = time.time()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        s = socket.create_connection((ip, 443), timeout=timeout)
    except ConnectionRefusedError:
        return "REFUSED at connect"
    except socket.timeout:
        return "TIMEOUT at connect"
    except OSError as e:
        return "ERR at connect: %s" % e
    t_conn = time.time()
    try:
        ss = ctx.wrap_socket(s, server_hostname=HOST)
    except Exception as e:
        s.close()
        return "TLS FAILED after %.0fms: %s" % ((time.time() - t) * 1000, e)
    t_tls = time.time()
    method = "HEAD" if head_only else "GET"
    req = ("%s %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: %s\r\nAccept: */*\r\n"
           "Connection: close\r\n\r\n" % (method, path, HOST, ua))
    try:
        ss.sendall(req.encode())
        buf = b""
        t_first = None
        while True:
            chunk = ss.recv(65536)
            if not chunk:
                break
            if t_first is None:
                t_first = time.time()
            buf += chunk
    except socket.timeout:
        ss.close()
        return ("STALLED: connected %.0fms, tls %.0fms, then no complete "
                "reply within %ss (got %d bytes)"
                % ((t_conn - t) * 1000, (t_tls - t_conn) * 1000, timeout,
                   len(buf)))
    except Exception as e:
        ss.close()
        return "ERR mid-request: %s" % e
    ss.close()
    head, _, body = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    keep = [l for l in lines[1:] if l.split(":")[0].lower() in (
        "server", "x-powered-by", "x-aspnet-version", "content-type",
        "content-length", "retry-after", "x-iinfo", "cf-ray", "x-cache",
        "x-azure-ref", "x-cdn", "via", "x-sucuri-id", "akamai-grn",
        "x-frame-options")]
    cookies = [l.split(":", 1)[1].split("=")[0].strip()
               for l in lines[1:] if l.lower().startswith("set-cookie")]
    return ("%s | conn %.0fms tls %.0fms ttfb %.0fms total %.0fms | %d bytes"
            " | %s | cookies=%s"
            % (lines[0] if lines else "?", (t_conn - t) * 1000,
               (t_tls - t_conn) * 1000,
               ((t_first or time.time()) - t_tls) * 1000,
               (time.time() - t) * 1000, len(body), "; ".join(keep),
               ",".join(cookies) or "-"))


def main():
    label = os.environ.get("PROBE_LABEL", "local")
    out("probe %s at %s" % (label, datetime.now(timezone.utc).isoformat()))

    # ---- who are we ----------------------------------------------------
    out("== runner identity")
    out("public IP : " + sh("curl -s -m 10 https://api.ipify.org"))
    out("ip info   : " + sh("curl -s -m 10 https://ipinfo.io/json "
                            "| tr -d '\\n' | cut -c1-300"))

    # ---- DNS -----------------------------------------------------------
    out("== DNS")
    ips = []
    for fam in (socket.AF_INET, socket.AF_INET6):
        try:
            for r in socket.getaddrinfo(HOST, 443, fam, socket.SOCK_STREAM):
                if r[4][0] not in ips:
                    ips.append(r[4][0])
        except socket.gaierror as e:
            out("getaddrinfo %s: %s" % (fam, e))
    out("system resolver: %s" % ips)
    for resolver in ("", "@8.8.8.8", "@1.1.1.1", "@9.9.9.9"):
        out("dig %-9s A   : %s" % (resolver or "default",
            sh("dig +noall +answer %s %s A" % (resolver, HOST))
            .replace("\n", " || ")))
    out("dig AAAA         : " + sh("dig +short %s AAAA" % HOST))
    out("dig NS           : " + sh("dig +short bccourts.ca NS")
        .replace("\n", " "))
    for ip in [i for i in ips if ":" not in i]:
        out("reverse %s : %s" % (ip, sh("dig +short -x %s" % ip)))
        out("whois %s   : %s" % (ip, sh(
            "whois %s | grep -iE '^(orgname|org-name|netname|descr|"
            "country|owner)' | head -5 | tr '\\n' ' '" % ip, 20)))

    # ---- per-address TCP ------------------------------------------------
    out("== TCP connect, 10 tries per address, 1s apart")
    for ip in ips:
        res = []
        for _ in range(10):
            res.append(tcp(ip))
            time.sleep(1)
        out("%-40s %s" % (ip, " | ".join(res)))
    for ip in [i for i in ips if ":" not in i]:
        out("port 80 %s : %s" % (ip, tcp(ip, 80)))

    # ---- per-address HTTP, bot vs browser ------------------------------
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import scrape
    out("== HTTPS GET of the index, per address")
    for ip in ips:
        out("%s bot     : %s" % (ip, raw_get(ip, INDEX_PATH,
                                             scrape.USER_AGENT)))
        time.sleep(2)
        out("%s browser : %s" % (ip, raw_get(ip, INDEX_PATH, BROWSER_UA)))
        time.sleep(2)

    # ---- path ----------------------------------------------------------
    out("== route to the court (TCP 443)")
    out(sh("sudo traceroute -T -p 443 -w 2 -q 1 -m 25 %s 2>&1 | tail -15"
           % HOST, 90))

    # ---- burst, exactly as the scraper does it --------------------------
    out("== burst: index + every PDF, one session, 2s apart (as scrape.py)")
    out("tls: " + str(scrape.ensure_tls(HOST)))
    session = scrape.make_session()
    t = time.time()
    try:
        r = session.get(scrape.INDEX_URL, timeout=30)
        out("index: HTTP %s %d bytes %.0fms"
            % (r.status_code, len(r.content), (time.time() - t) * 1000))
        docs = scrape.collect_links(r.text, r.url)
    except Exception as e:
        out("index FAILED: %s" % e)
        docs = []
    out("documents found: %d" % len(docs))
    fails = 0
    for i, d in enumerate(docs, 1):
        time.sleep(2)
        t = time.time()
        try:
            r = session.get(d["url"], timeout=30)
            out("%2d %s %7d bytes %5.0fms  %s"
                % (i, r.status_code, len(r.content),
                   (time.time() - t) * 1000, d["title"]))
        except Exception as e:
            fails += 1
            out("%2d FAILED %5.0fms %s: %s"
                % (i, (time.time() - t) * 1000, d["title"],
                   str(e)[:200]))
    out("burst failures: %d of %d" % (fails, len(docs)))

    # ---- after the burst, are addresses still reachable? ---------------
    out("== TCP after burst")
    for ip in ips:
        out("%-40s %s" % (ip, " | ".join(tcp(ip) for _ in range(5))))

    out("done")


if __name__ == "__main__":
    main()
