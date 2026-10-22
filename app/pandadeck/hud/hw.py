"""Hardware names for the HUD: a short host name, CPU and GPU model.

    "MrDesk.corp.example.com"                     -> "MrDesk"
    "Intel(R) Core(TM) i5-10400 CPU @ 2.90GHz"    -> "i5-10400"
    "AMD Ryzen 7 7700X 8-Core Processor"          -> "Ryzen 7 7700X"
    "Apple M3 Pro"                                -> "M3 Pro"
    "NVIDIA GeForce RTX 4060 Laptop GPU"          -> "RTX 4060"
    "AMD Radeon RX 7800 XT"                       -> "RX 7800 XT"

The short_* functions are pure (unit-tested); cpu_brand() asks the OS.
"""
from __future__ import annotations

import platform
import re
import socket
import subprocess
import sys

from ..deck.grammar import ascii_label

MODEL_MAX = 23  # what the panel shows next to the name

_CPU_PATTERNS = (
    r"\b(i[3579]-\d{3,5}[A-Z]{0,3})\b",                 # Intel Core i5-10400
    r"\b(Ultra \d \d{3}[A-Z]{0,2})\b",                  # Intel Core Ultra 7 155H
    r"\b(Threadripper (?:PRO )?\d{4}\w*)",              # AMD Ryzen Threadripper 3970X
    r"\b(Ryzen AI \d+ (?:HX |PRO )?\d{3}\w*)",          # AMD Ryzen AI 9 HX 370
    r"\b(Ryzen \d (?:PRO )?\d{4}\w*)",                  # AMD Ryzen 7 7700X / Ryzen 5 PRO 4650G
    r"\b(Xeon\b.*?[A-Z]?-?\d{4}\w*(?: v\d)?)",          # Intel Xeon E5-2680 v4 / Gold 6338
    r"\b(EPYC \d{4}\w*)",                               # AMD EPYC 7543
    r"\b(M\d(?: (?:Pro|Max|Ultra))?)\b",                # Apple M3 Pro
    r"\b(Raspberry Pi \d\w*(?: Model \w)?)",            # Raspberry Pi 4 Model B
    r"\b(Celeron \w+|Pentium \w+|Atom \w+|N\d{2,3})\b",  # small Intel parts
    r"\b(Snapdragon \w+(?: \w+)?)",                     # Windows on ARM
)
_GPU_RE = re.compile(r"\b((?:RTX|GTX|RX|Arc)\s*(?:PRO\s+)?[A-Z]?\d{3,4}\w*(?:\s+(?:Ti|SUPER|XTX|XT|GRE))*)",
                     re.IGNORECASE)


def clean_host_name(hostname: str, limit: int = 31) -> str:
    """Short display name from a host name: no domain, ASCII, bounded."""
    name = (hostname or "").strip().split(".")[0]
    return ascii_label(name, limit) or "This PC"


def short_cpu(brand: str) -> str:
    text = re.sub(r"\((?:R|TM|tm|r)\)", "", brand or "").replace(" CPU", " ")
    text = re.sub(r"\s+", " ", text).strip()
    for pat in _CPU_PATTERNS:
        m = re.search(pat, text)
        if m:
            return ascii_label(m.group(1), MODEL_MAX)
    text = re.sub(r"@.*$", "", text)
    for drop in ("Intel", "AMD", "Apple", "Core", "CPU", "Processor", "processor", "with Radeon Graphics"):
        text = text.replace(drop, " ")
    text = re.sub(r"\d+-Core", " ", text)
    return ascii_label(re.sub(r"\s+", " ", text).strip(), MODEL_MAX)


def short_gpu(name: str) -> str:
    text = re.sub(r"\((?:R|TM)\)", "", name or "")
    m = _GPU_RE.search(text)
    if m:
        return ascii_label(re.sub(r"\s+", " ", m.group(1)).strip(), MODEL_MAX)
    for drop in ("NVIDIA", "GeForce", "AMD", "Radeon", "Graphics", "Laptop GPU", "GPU", "Intel"):
        text = text.replace(drop, " ")
    return ascii_label(re.sub(r"\s+", " ", text).strip(), MODEL_MAX)


def cpu_brand() -> str:
    """The CPU brand string from the OS (best effort, "" when unknown)."""
    try:
        if sys.platform == "win32":
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                                 timeout=3, check=False).stdout.strip()
            if out:
                return out
        else:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
                info = f.read()
            for key in ("Model", "model name", "Hardware", "cpu model"):  # "Model" = Raspberry Pi board
                m = re.search(rf"^{key}\s*:\s*(.+)$", info, re.MULTILINE)
                if m:
                    return m.group(1).strip()
    except Exception:  # noqa: BLE001 - registry/sysctl/proc missing: fall through
        pass
    return platform.processor() or ""


def host_name() -> str:
    return clean_host_name(socket.gethostname())
