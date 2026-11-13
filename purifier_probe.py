"""
purifier_probe.py — can this PC read your Mi purifier directly over Wi-Fi?

Step 1 proves local communication (model + firmware).
Step 2 tries to read PM2.5 using the MiOT property IDs common on Mi purifiers, and prints
the raw answers, so we can see which one is the PM2.5 reading for your model.

Needs:  pip install python-miio
        the purifier's IP address and its 32-character token (see README in chat / below)

Run:    python purifier_probe.py --ip 192.168.1.50 --token 0123456789abcdef0123456789abcdef
"""
import argparse
import json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", required=True)
    ap.add_argument("--token", required=True)
    a = ap.parse_args()
    try:
        from miio import Device
    except ImportError:
        raise SystemExit("python-miio isn't installed. Run:  pip install python-miio")

    dev = Device(a.ip, a.token)
    print("STEP 1 — talking to the purifier …")
    try:
        info = dev.info()
        print(f"  OK  model={info.model}  firmware={info.firmware_version}  hardware={info.hardware_version}")
    except Exception as e:
        raise SystemExit(f"  FAILED: {e}\n  Check: same Wi-Fi network, correct IP, correct token "
                         "(a token changes if the purifier is reset or re-paired).")

    print("\nSTEP 2 — reading properties (looking for PM2.5) …")
    # (service id, property id) pairs used by Mi purifiers for air-quality readings
    candidates = [(3, 4), (3, 1), (3, 7), (2, 1), (2, 2), (2, 4), (2, 5), (2, 6),   # air + power/mode/fan
                  (4, 1), (4, 2), (4, 3), (4, 4), (4, 5),                             # filter service
                  (9, 1), (9, 2), (9, 3), (9, 4), (9, 5), (9, 11), (13, 2)]           # misc / motor / display
    req = [{"did": f"{s}-{p}", "siid": s, "piid": p} for s, p in candidates]
    try:
        res = dev.send("get_properties", req)
        for r in res:
            v = r.get("value")
            flag = ""
            print(f"  siid {r.get('siid'):>2} piid {r.get('piid'):>2}: code {r.get('code')}  value {v}{flag}")
        print("\nRaw answer (send this to Claude):")
        print(json.dumps(res))
    except Exception as e:
        print(f"  MiOT read failed: {e}")
        try:
            print("  Trying older protocol …", dev.send("get_prop", ["aqi", "pm25"]))
        except Exception as e2:
            print(f"  Older protocol failed too: {e2}")


if __name__ == "__main__":
    main()
