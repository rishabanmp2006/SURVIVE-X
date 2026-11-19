"""Headless Linux application loop for the UNO Q Bridge prototype."""
import argparse
import journey
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def check_runtime(server, model):
    if not Path(model).is_file():
        raise ValueError('Model file does not exist')
    try:
        with open(model, 'rb') as gguf:
            if gguf.read(4) != b'GGUF':
                raise ValueError('Model is not a GGUF file')
    except OSError as exc:
        raise ValueError(f'Cannot read model: {exc}') from exc
    if not os.access(server,os.X_OK):
        raise ValueError('llama-server is not executable')
    try:
        help_result=subprocess.run([server,'--help'],capture_output=True,text=True,timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f'Unable to run llama-server: {exc}') from exc
    if help_result.returncode:
        raise ValueError('llama-server --help failed')
    missing=[flag for flag in ('--reasoning','--parallel','--host','--port','--ctx-size')
             if flag not in help_result.stdout+help_result.stderr]
    if missing:
        raise ValueError('llama-server lacks required options: '+', '.join(missing))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',required=True)
    parser.add_argument('--llama-server',required=True)
    parser.add_argument('--database',required=True)
    parser.add_argument('--waypoints',required=True)
    parser.add_argument('--declination',type=float,help='verified local east-positive magnetic declination in degrees')
    parser.add_argument('--gnss-port',help='explicit GNSS NMEA serial device, if installed')
    parser.add_argument('--gnss-baud',type=int,default=9600)
    parser.add_argument('--check',action='store_true',help='validate configured files and llama-server options, then exit')
    journey.add_options(parser)
    args=parser.parse_args()
    if args.check:
        try:
            check_runtime(args.llama_server,args.model)
            if not Path(args.database).is_file(): raise ValueError('Places database does not exist')
        except ValueError as exc:
            print(f'Preflight failed: {exc}',file=sys.stderr)
            return 1
        print('Files and llama-server options passed preflight; model loading has not been tested.')
        return 0
    if not Path(args.database).is_file():
        print('Places database does not exist',file=sys.stderr)
        return 1
    os.environ['SURVIVEX_MODEL']=args.model
    os.environ['SURVIVEX_LLAMA_SERVER']=args.llama_server
    import navigation
    from app import SURVIVEXApp
    from hardware_sensors import LiveSensors, NMEAReader
    from unoq_bridge import BridgeLink
    sensors=LiveSensors()
    reader=NMEAReader(sensors,args.gnss_port,args.gnss_baud) if args.gnss_port else None
    ui=SURVIVEXApp(storage_path=args.waypoints,db_path=args.database,async_search=True,
                    live_sensors=sensors,live_declination=args.declination,recorder=journey.configured(args))
    ui.sensor_mode=navigation.SensorMode.LIVE_GNSS
    link=BridgeLink()
    if reader: reader.start()
    running=True
    def stop(_signum,_frame):
        nonlocal running
        running=False
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    try:
        last_error = None
        while running and ui.running:
            link.update(ui,ui.tick())
            if link.error != last_error:
                if link.error:
                    print(f'Bridge: {link.error}',file=sys.stderr,flush=True)
                elif last_error:
                    print('Bridge: connected',file=sys.stderr,flush=True)
                last_error = link.error
            time.sleep(0.02)
    finally:
        if reader: reader.close()
        link.close()
        ui.close()
    return 0


if __name__=='__main__':
    sys.exit(main())
