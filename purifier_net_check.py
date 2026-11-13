"""
purifier_net_check.py — why can't this PC reach the purifier? (no token needed)

Checks, in order:
  1. this PC's local IP — is it on the same network (same first three numbers) as the purifier?
  2. ping — does the purifier answer at all at that IP?
  3. miIO "hello" on UDP port 54321 — the exact packet python-miio sends first. A reply means
     local control is possible and the problem is the token; no reply means the network,
     a firewall, or the device itself is blocking it.
  4. broadcast hello — lists every Xiaomi device on this network that answers, with its IP
     (catches the case where the purifier's IP has changed since the cloud last saw it).

Run:  python purifier_net_check.py 192.168.1.50
"""
import socket
import subprocess
import sys
import time

HELLO = bytes.fromhex("21310020" + "ff" * 28)     # standard miIO discovery packet


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))                 # no packet is sent; just picks the outgoing interface
        return s.getsockname()[0]
    finally:
        s.close()


def hello(addr, broadcast=False, wait=3.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if broadcast:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(wait)
    found = {}
    try:
        for _ in range(3):
            s.sendto(HELLO, (addr, 54321))
            end = time.time() + wait
            while time.time() < end:
                try:
                    data, src = s.recvfrom(1024)
                except socket.timeout:
                    break
                if len(data) >= 32 and data[:2] == b"\x21\x31":
                    found[src[0]] = data[8:12].hex()   # device id
            if found and not broadcast:
                break
    finally:
        s.close()
    return found


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python purifier_net_check.py <purifier IP>")
    ip = sys.argv[1]
    me = local_ip()
    same = me.rsplit(".", 1)[0] == ip.rsplit(".", 1)[0]
    print(f"1. This PC: {me}   purifier: {ip}   → {'same network' if same else 'DIFFERENT network ranges!'}")

    r = subprocess.run(["ping", "-n", "3", "-w", "1500", ip], capture_output=True, text=True)
    ok = "TTL=" in r.stdout.upper()
    print(f"2. Ping: {'answers' if ok else 'NO ANSWER'}")

    h = hello(ip)
    print(f"3. miIO hello to {ip}:54321: " + (f"REPLY (device id {h[ip]}) — local control works; "
          "if the probe still fails, the token is wrong/old" if ip in h else "no reply"))

    b = hello("255.255.255.255", broadcast=True)
    print("4. Xiaomi devices answering on this network: " +
          (", ".join(f"{a} (id {d})" for a, d in b.items()) if b else "none"))

    arp = subprocess.run(["arp", "-a", ip], capture_output=True, text=True).stdout
    mac = next((ln.split()[1] for ln in arp.splitlines() if ln.strip().startswith(ip)), None)
    print(f"5. This PC's ARP entry for {ip}: {mac or 'none'}  (compare with the purifier MAC from the extractor)")

    print("\nWhat it means:")
    if ip in h:
        print("  Network is fine. Re-run the extractor and copy the token again (it changes after a reset/re-pair).")
    elif ip in b:
        print("  The purifier answers a BROADCAST but not messages sent straight to its IP. That is a\n"
              "  delivery problem on the local network, not the token. Usual causes: this PC has a stale\n"
              "  ARP entry (MAC in 5 differs from the purifier's MAC — another device used this IP before),\n"
              "  or the router isolates Wi-Fi clients for direct traffic. Fix the first with, in an\n"
              "  Administrator terminal:  arp -d " + ip + "   then run this check again.")
    elif b:
        print("  Xiaomi devices do answer, just not at that IP — the purifier's IP has probably changed.\n"
              "  Try the probe with the IP(s) listed in 4 (compare the device id with the extractor's 'ID').")
    elif ok:
        print("  The purifier answers ping but not miIO. Most likely Windows/Bitdefender firewall is blocking\n"
              "  Python's UDP traffic, or this model/firmware has local control disabled.")
    elif not same:
        print("  The PC and purifier are on different networks (e.g. guest Wi-Fi, a second router or mesh\n"
              "  node in router mode). Connect the PC to the same network as the purifier.")
    else:
        print("  Nothing answers at that IP — the purifier may be off/asleep, on another IP, or the router\n"
              "  has 'AP/client isolation' on, which stops Wi-Fi devices talking to each other.")


if __name__ == "__main__":
    main()
