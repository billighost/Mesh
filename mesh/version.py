import threading

VERSION = "0.4.4"
PACKAGE_NAME = "mesh-context-layer"

def get_latest_version():
    """Check PyPI for the latest version of the package."""
    try:
        import httpx
        url = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"
        response = httpx.get(url, timeout=1.5)
        if response.status_code == 200:
            return response.json()["info"]["version"]
    except Exception:
        pass
    return None

def _run_check():
    latest = get_latest_version()
    if not latest:
        return
        
    # Simple semver compare (only notify if latest > VERSION)
    try:
        l_parts = [int(p) for p in latest.split(".")]
        v_parts = [int(p) for p in VERSION.split(".")]
        if l_parts > v_parts:
            print(f"\n[UPDATE] A new version of Mesh is available: {latest} (you have {VERSION})")
            print(f"   Run 'pip install --upgrade {PACKAGE_NAME}' to update.\n")
    except (ValueError, IndexError):
        # Fallback to string compare if semver fails
        if latest != VERSION:
             print(f"\n[UPDATE] Version {latest} is available on PyPI (you have {VERSION})\n")

def check_for_updates():
    """Check for updates in a background thread to avoid blocking CLI startup."""
    thread = threading.Thread(target=_run_check, daemon=True)
    thread.start()
