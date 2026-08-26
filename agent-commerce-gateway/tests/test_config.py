import subprocess
import sys
from pathlib import Path

def test_missing_config_fails_fast(monkeypatch, tmp_path):
    # Ensure no environment variables or .env file are picked up
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    
    # Run config.py in a subprocess without .env
    app_dir = Path(__file__).parent.parent
    
    # Run with empty environment AND from a temp directory that has no .env file,
    # so pydantic-settings cannot find the project's .env.
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, r'" + str(Path(__file__).parent.parent) + "'); import app.config"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env={}
    )
    
    assert result.returncode == 1
    assert "Configuration Error: Missing or invalid required environment variables." in result.stdout
    assert "razorpay_key_id" in result.stdout
    assert "razorpay_key_secret" in result.stdout
