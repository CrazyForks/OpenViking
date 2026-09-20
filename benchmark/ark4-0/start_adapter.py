#!/usr/bin/env python3
"""Start the direct Viking adapter using the adjacent unified config file."""

import argparse
import os
from pathlib import Path

from adapter_settings import load_settings, proxy_config

SCRIPT_DIR = Path(__file__).resolve().parent


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(SCRIPT_DIR / "adapter_config.local.json"))
    return parser.parse_args()


def main():
    path = Path(parse_args().config).expanduser().resolve()
    config = load_settings(path)
    ov_config = config.memory_proxy.get("openviking_config_file")
    if ov_config:
        os.environ["OPENVIKING_CONFIG_FILE"] = str(path.parent / Path(ov_config).expanduser())
    import uvicorn
    from viking_client import VikingClient
    from viking_service import create_app

    app = create_app(
        VikingClient(config.viking.base_url, config.viking.api_token),
        config,
        state_dir=path.parent / config.service.state_dir,
        memory_proxy=proxy_config(config, path),
    )
    uvicorn.run(
        app, host=config.service.host, port=config.service.port, log_level=config.service.log_level
    )


if __name__ == "__main__":
    main()
