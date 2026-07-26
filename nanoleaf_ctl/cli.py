"""CLI entry point for nanoleaf-ctl.

Usage:
    nanoleaf-ctl discover          -- find devices on the network
    nanoleaf-ctl pair <ip>         -- pair using the model's official instructions
    nanoleaf-ctl info              -- show device info
    nanoleaf-ctl on                -- turn on
    nanoleaf-ctl off               -- turn off
    nanoleaf-ctl toggle            -- toggle power
    nanoleaf-ctl brightness <0-100>  -- set brightness
    nanoleaf-ctl color <color>     -- set color (hex, rgb, or name)
    nanoleaf-ctl color-temp <1200-6500> -- set color temperature
    nanoleaf-ctl effects           -- list available effects
    nanoleaf-ctl effect <name>     -- activate an effect
    nanoleaf-ctl sunlight            -- run window light simulator
    nanoleaf-ctl sunlight --lat 40.7 --lon -74 --facing south
    nanoleaf-ctl sunlight --preview  -- show full day without running
    nanoleaf-ctl web               -- start web interface for browser/phone
    nanoleaf-ctl identify          -- flash the panels to identify them
    nanoleaf-ctl forget            -- remove saved device config
"""

import argparse
import os
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from nanoleaf_ctl import client, config, sunlight, web
from nanoleaf_ctl.weather import WeatherCache


def cmd_discover(args):
    print("Scanning for Nanoleaf devices...")
    devices = client.discover_devices(timeout=args.timeout)
    if not devices:
        print("No devices found. Make sure you're on the same network.")
        return 1
    print(f"Found {len(devices)} device(s):")
    for ip in devices:
        print(f"  {ip}")
    return 0


def cmd_pair(args):
    print(f"Pairing with {args.ip}...")
    print("Follow Nanoleaf's instructions for this exact model before pairing:")
    print("https://support.nanoleaf.me/hc/en-us")
    print("This independent third-party project is not affiliated with Nanoleaf.")
    try:
        client.pair(args.ip)
        print("Paired successfully. Auth token saved with private permissions.")
    except Exception as e:
        print(f"Pairing failed: {e}")
        print("Ensure the device is in pairing mode.")
        return 1
    return 0


def cmd_setup(args):
    config.save_device(args.ip, args.token)
    print(f"Saved device config (ip={args.ip}).")
    # Verify the connection works
    try:
        nl = client.connect(args.ip)
        info = client.get_info(nl)
        print(f"Connected to {info['name']}!")
    except Exception as e:
        print(f"Warning: saved config but could not connect: {e}")
    return 0


def cmd_info(args):
    nl = client.connect(args.ip)
    info = client.get_info(nl)
    print(f"  Name:       {info['name']}")
    print(f"  Model:      {info['model']}")
    print(f"  Serial:     {info['serial']}")
    print(f"  Firmware:   {info['firmware']}")
    print(f"  Panels:     {info['num_panels']}")
    print(f"  Power:      {'on' if info['power'] else 'off'}")
    print(f"  Brightness: {info['brightness']}%")
    print(f"  Effect:     {info['effect']}")
    return 0


def cmd_on(args):
    nl = client.connect(args.ip)
    client.set_power(nl, True)
    print("Powered on.")
    return 0


def cmd_off(args):
    nl = client.connect(args.ip)
    client.set_power(nl, False)
    print("Powered off.")
    return 0


def cmd_toggle(args):
    nl = client.connect(args.ip)
    state = "on" if client.toggle_power(nl) else "off"
    print(f"Toggled. Now {state}.")
    return 0


def cmd_brightness(args):
    nl = client.connect(args.ip)
    level = args.level
    if level < 0 or level > 100:
        print("Brightness must be between 0 and 100.")
        return 1
    client.set_brightness(nl, level)
    print(f"Brightness set to {level}%.")
    return 0


def cmd_color(args):
    nl = client.connect(args.ip)
    color_str = " ".join(args.color)
    try:
        client.set_color_from_string(nl, color_str)
        print(f"Color set to '{color_str}'.")
    except ValueError as e:
        print(str(e))
        return 1
    return 0


def cmd_color_temp(args):
    nl = client.connect(args.ip)
    temp = args.temp
    if temp < 1200 or temp > 6500:
        print("Color temperature must be between 1200 and 6500.")
        return 1
    client.set_color_temp(nl, temp)
    print(f"Color temperature set to {temp}K.")
    return 0


def cmd_effects(args):
    nl = client.connect(args.ip)
    effects = client.list_effects(nl)
    current = client.get_current_effect(nl)
    print(f"Available effects ({len(effects)}):")
    for name in sorted(effects):
        marker = " *" if name == current else ""
        print(f"  {name}{marker}")
    return 0


def cmd_effect(args):
    nl = client.connect(args.ip)
    name = args.name
    available = client.list_effects(nl)
    if name not in available:
        print(f"Effect '{name}' not found. Available effects:")
        for eff in sorted(available):
            print(f"  {eff}")
        return 1
    client.set_effect(nl, name)
    print(f"Effect set to '{name}'.")
    return 0


def cmd_sunlight(args):
    cfg = sunlight.WindowConfig(
        latitude=args.lat,
        longitude=args.lon,
        timezone=args.tz,
        facing=args.facing,
        peak_brightness=args.peak,
        night_off=not args.night_glow,
    )

    if args.preview:
        print(f"Window light preview ({args.facing}-facing, {args.lat:.1f}N {args.lon:.1f}E)")
        print(f"  {'Time':>6}  {'Phase':<14} {'Mode':<10} {'Value':<18} {'Bright':>6}")
        print(f"  {'----':>6}  {'-----':<14} {'----':<10} {'-----':<18} {'------':>6}")
        for state in sunlight.preview_day(cfg):
            mode = state["mode"]
            if mode == "color":
                val = f"RGB {state['rgb']}"
            elif mode == "color_temp":
                val = f"{state['color_temp']}K"
            else:
                val = "off"
            print(f"  {state['time']:>6}  {state['phase']:<14} {mode:<10} {val:<18} {state['brightness']:>5}%")
        return 0

    # Prevent duplicate instances — this is the #1 cause of "pulsing" light
    lock_fd = config.acquire_sunlight_lock()
    if lock_fd is None:
        holder = config.read_lock_info() or "unknown"
        print(f"Another sunlight instance is already running (held by {holder}).")
        print(f"Lock file: {config.LOCK_FILE}")
        print("Kill the other instance first, or stop it from the web UI.")
        return 1

    # Set up logging — to file if --log specified, otherwise stdout
    log_file = None
    if args.log:
        log_path = Path(args.log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "a")

    def _out(msg):
        if log_file:
            log_file.write(msg + "\n")
            log_file.flush()
        else:
            print(msg)

    nl = client.connect(args.ip)

    # Set up weather if enabled
    weather_cache = None
    if not args.no_weather:
        weather_cache = WeatherCache(args.lat, args.lon)
        _out("Weather integration enabled (via Open-Meteo, updates every 10 min)")

    def _format_state(state):
        mode = state["mode"]
        if mode == "color":
            val = f"RGB {state['rgb']}"
        elif mode == "color_temp":
            val = f"{state['color_temp']}K"
        else:
            val = "off"
        # Append weather info if present
        weather_str = state.get("weather", "")
        cloud = state.get("cloud_cover")
        if weather_str:
            val += f"  [{weather_str} {cloud}%]"
        return val

    def on_update(state):
        now = datetime.now().strftime("%H:%M")
        _out(f"  [{now}] {state['phase']:<14} {_format_state(state):<30} {state['brightness']}%")

    def on_disconnect(err):
        now = datetime.now().strftime("%H:%M")
        _out(f"  [{now}] Device offline (light switch off?). Waiting for it to come back...")

    def on_reconnect(state):
        now = datetime.now().strftime("%H:%M")
        _out(f"  [{now}] Device back online! Resuming: {state['phase']:<14} {_format_state(state):<30} {state['brightness']}%")

    _out(f"Window light simulator ({args.facing}-facing)")
    _out(f"Host: {socket.gethostname()} (PID {os.getpid()})")
    _out(f"Location: {args.lat:.2f}N, {args.lon:.2f}E ({args.tz})")
    _out(f"Peak brightness: {args.peak}%. Updating every {args.interval}s.")
    _out("")
    try:
        sunlight.run_simulator(
            nl, cfg,
            update_interval=args.interval,
            weather_cache=weather_cache,
            on_update=on_update,
            on_disconnect=on_disconnect,
            on_reconnect=on_reconnect,
        )
    except KeyboardInterrupt:
        _out("\nWindow light simulator stopped.")
    finally:
        if log_file:
            log_file.close()
        config.release_sunlight_lock(lock_fd)
    return 0


def cmd_identify(args):
    nl = client.connect(args.ip)
    client.identify(nl)
    print("Identifying... (panels should flash)")
    return 0


TASK_NAME = "NanoleafSunlight"


def cmd_install(args):
    """Install the sunlight simulator as a Windows startup task."""
    if sys.platform != "win32":
        print("Auto-start install is currently Windows-only.")
        print("On Linux, create a systemd user service or add to crontab @reboot.")
        return 1

    # Find pythonw.exe (windowless Python) next to python.exe
    python_dir = Path(sys.executable).parent
    pythonw = python_dir / "pythonw.exe"
    if not pythonw.exists():
        # Fall back to python.exe if pythonw not found
        pythonw = Path(sys.executable)

    log_path = Path.home() / ".config" / "nanoleaf-ctl" / "sunlight.log"

    # Build the command that Task Scheduler will run
    cmd_args = [
        str(pythonw), "-m", "nanoleaf_ctl.cli", "sunlight",
        "--log", str(log_path),
    ]
    # Pass through sunlight options if non-default
    if args.lat != sunlight.DEFAULT_LAT:
        cmd_args.extend(["--lat", str(args.lat)])
    if args.lon != sunlight.DEFAULT_LON:
        cmd_args.extend(["--lon", str(args.lon)])
    if args.facing != sunlight.DEFAULT_FACING:
        cmd_args.extend(["--facing", args.facing])
    if args.peak != sunlight.DEFAULT_PEAK:
        cmd_args.extend(["--peak", str(args.peak)])
    if args.night_glow:
        cmd_args.append("--night-glow")
    if args.no_weather:
        cmd_args.append("--no-weather")

    command_str = subprocess.list2cmdline(cmd_args)

    # Create scheduled task that runs at user logon
    schtasks_cmd = [
        "schtasks", "/create",
        "/tn", TASK_NAME,
        "/tr", command_str,
        "/sc", "onlogon",
        "/rl", "limited",
        "/f",  # force overwrite if exists
    ]

    try:
        subprocess.run(schtasks_cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"Failed to create scheduled task: {e.stderr}")
        return 1

    print(f"Installed '{TASK_NAME}' to run at logon.")
    print(f"  Command: {command_str}")
    print(f"  Log:     {log_path}")
    print()
    print("To start it right now without rebooting:")
    print(f"  schtasks /run /tn {TASK_NAME}")
    print()
    print("To remove:")
    print(f"  nanoleaf-ctl uninstall")
    return 0


def cmd_uninstall(_args):
    """Remove the sunlight simulator startup task."""
    if sys.platform != "win32":
        print("Auto-start uninstall is currently Windows-only.")
        return 1

    try:
        subprocess.run(
            ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
            check=True, capture_output=True, text=True,
        )
        print(f"Removed '{TASK_NAME}' startup task.")
    except subprocess.CalledProcessError:
        print(f"Task '{TASK_NAME}' not found (already removed?).")
    return 0


def cmd_web(args):
    port = args.port
    print(f"Starting web interface on http://0.0.0.0:{port}")
    print(f"Open in your browser (or on your phone on the same network).")
    web.run(host="0.0.0.0", port=port, ip=args.ip)
    return 0


def cmd_forget(_args):
    config.clear()
    print("Saved device configuration removed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nanoleaf-ctl",
        description="Control Nanoleaf panels from the command line.",
    )
    parser.add_argument(
        "--ip",
        default=None,
        help="Device IP (uses saved config if omitted)",
    )
    sub = parser.add_subparsers(dest="command")

    # discover
    p = sub.add_parser("discover", help="Find Nanoleaf devices on the network")
    p.add_argument("--timeout", type=int, default=5, help="Search timeout in seconds")

    # pair
    p = sub.add_parser("pair", help="Pair with a device")
    p.add_argument("ip", metavar="IP", help="Device IP address")

    # setup
    p = sub.add_parser("setup", help="Save IP and auth token from a previous pairing")
    p.add_argument("ip", metavar="IP", help="Device IP address")
    p.add_argument("token", metavar="TOKEN", help="Auth token")

    # info
    sub.add_parser("info", help="Show device info")

    # power
    sub.add_parser("on", help="Turn on")
    sub.add_parser("off", help="Turn off")
    sub.add_parser("toggle", help="Toggle power")

    # brightness
    p = sub.add_parser("brightness", help="Set brightness (0-100)")
    p.add_argument("level", type=int, help="Brightness level")

    # color
    p = sub.add_parser("color", help="Set color (hex, RGB, or name)")
    p.add_argument("color", nargs="+", help="Color value")

    # color-temp
    p = sub.add_parser("color-temp", help="Set color temperature (1200-6500)")
    p.add_argument("temp", type=int, help="Color temperature in Kelvin")

    # effects
    sub.add_parser("effects", help="List available effects")

    # effect
    p = sub.add_parser("effect", help="Activate an effect")
    p.add_argument("name", help="Effect name")

    # sunlight
    p = sub.add_parser("sunlight", help="Run window light simulator")
    p.add_argument("--lat", type=float, default=sunlight.DEFAULT_LAT, help=f"Latitude (default {sunlight.DEFAULT_LAT})")
    p.add_argument("--lon", type=float, default=sunlight.DEFAULT_LON, help=f"Longitude (default {sunlight.DEFAULT_LON})")
    p.add_argument("--tz", default=sunlight.DEFAULT_TZ, help=f"Timezone (default {sunlight.DEFAULT_TZ})")
    p.add_argument("--facing", default=sunlight.DEFAULT_FACING,
                    choices=["north", "northeast", "east", "southeast",
                             "south", "southwest", "west", "northwest"],
                    help=f"Window orientation (default {sunlight.DEFAULT_FACING})")
    p.add_argument("--peak", type=int, default=sunlight.DEFAULT_PEAK, help=f"Peak brightness %% (default {sunlight.DEFAULT_PEAK})")
    p.add_argument("--night-glow", action="store_true", help="Keep a dim warm glow at night instead of off")
    p.add_argument("--interval", type=int, default=60, help="Update interval in seconds (default 60)")
    p.add_argument("--preview", action="store_true", help="Show full day schedule without running")
    p.add_argument("--no-weather", action="store_true", help="Disable real-time weather integration")
    p.add_argument("--log", metavar="FILE", help="Write output to log file (for background use)")

    # install
    p = sub.add_parser("install", help="Install sunlight simulator to run at Windows startup")
    p.add_argument("--lat", type=float, default=sunlight.DEFAULT_LAT, help="Latitude")
    p.add_argument("--lon", type=float, default=sunlight.DEFAULT_LON, help="Longitude")
    p.add_argument("--facing", default=sunlight.DEFAULT_FACING,
                    choices=["north", "northeast", "east", "southeast",
                             "south", "southwest", "west", "northwest"],
                    help="Window orientation")
    p.add_argument("--peak", type=int, default=sunlight.DEFAULT_PEAK, help="Peak brightness %%")
    p.add_argument("--night-glow", action="store_true", help="Keep dim warm glow at night")
    p.add_argument("--no-weather", action="store_true", help="Disable weather integration")

    # web
    p = sub.add_parser("web", help="Start web interface for browser/phone control")
    p.add_argument("--port", type=int, default=5000, help="Port to listen on (default 5000)")

    # uninstall
    sub.add_parser("uninstall", help="Remove sunlight simulator from Windows startup")

    # identify
    sub.add_parser("identify", help="Flash panels for identification")

    # forget
    sub.add_parser("forget", help="Remove saved device config")

    return parser


COMMANDS = {
    "discover": cmd_discover,
    "pair": cmd_pair,
    "setup": cmd_setup,
    "info": cmd_info,
    "on": cmd_on,
    "off": cmd_off,
    "toggle": cmd_toggle,
    "brightness": cmd_brightness,
    "color": cmd_color,
    "color-temp": cmd_color_temp,
    "effects": cmd_effects,
    "effect": cmd_effect,
    "sunlight": cmd_sunlight,
    "web": cmd_web,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "identify": cmd_identify,
    "forget": cmd_forget,
}


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    handler = COMMANDS.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    try:
        rc = handler(args)
        sys.exit(rc or 0)
    except ConnectionError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
