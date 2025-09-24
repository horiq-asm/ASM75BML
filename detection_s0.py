# basic_pipelines/detection_s.py
import os
import argparse
from pathlib import Path

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

import hailo

# ---- 既存の user_data 用クラス（簡易カウンタ） --------------------
class user_app_callback_class:
    def __init__(self):
        self._count = 0
    def increment(self):
        self._count += 1
    def get_count(self):
        return self._count

# ---- 既存のコールバック：hailofilter 後のバッファから検出結果を読む ---
def app_callback(pad, info, user_data):
    user_data.increment()
    s = [f"Frame count: {user_data.get_count()}"]
    buf = info.get_buffer()
    if buf is None:
        return Gst.PadProbeReturn.OK

    roi = hailo.get_roi_from_buffer(buf)
    for det in roi.get_objects_typed(hailo.HAILO_DETECTION):
        s.append(f"Detection: {det.get_label()} Confidence: {det.get_confidence():.2f}")
    print("\n".join(s))
    return Gst.PadProbeReturn.OK


def build_source_desc(src, latency):
    """RTSP なら rtspsrc→H.264 depay/parse→avdec_h264→RGB 640x640、ファイルなら decodebin→RGB 640x640"""
    if isinstance(src, str) and src.startswith("rtsp://"):
        return (
            f'rtspsrc location="{src}" latency={latency} protocols=tcp ! '
            'queue max-size-buffers=4 leaky=downstream ! '
            'rtph264depay ! h264parse config-interval=1 ! '
            'avdec_h264 ! '
            'videoconvert n-threads=2 ! videoscale n-threads=2 ! '
            'video/x-raw,format=RGB,width=640,height=640'
        )
    else:
        # ローカルファイル/USB 等は decodebin 任せ
        return (
            f'filesrc location="{src}" ! decodebin ! '
            'videoconvert n-threads=2 ! videoscale n-threads=2 ! '
            'video/x-raw,format=RGB,width=640,height=640'
        )


def main():
    # ---- .env の場所を環境変数で知らせる（必要なら） ---------------
    project_root = Path(__file__).resolve().parent.parent
    env_file = project_root / ".env"
    os.environ["HAILO_ENV_FILE"] = str(env_file)

    # ---- 引数 ------------------------------------------------------
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input", default="/usr/local/hailo/resources/videos/example_640.mp4",
                        help="RTSP URL またはローカル動画パス")
    parser.add_argument("--latency", type=int, default=100, help="rtspsrc の latency（ms）")
    parser.add_argument("--hef-path", default="/usr/local/hailo/resources/models/hailo8l/yolov8s.hef")
    parser.add_argument("--so-path",  default="/usr/local/hailo/resources/so/libyolo_hailortpp_postprocess.so")
    parser.add_argument("--function-name", default="yolov8s", help="hailofilter の function-name（lib 内のシンボル）")
    parser.add_argument("--score", type=float, default=0.25, help="hailonet nms-score-threshold")
    parser.add_argument("--iou",   type=float, default=0.45, help="hailonet nms-iou-threshold")
    parser.add_argument("--sink",  default="waylandsink sync=false", help="表示シンク（例: waylandsink sync=false）")
    args = parser.parse_args()

    # ---- GStreamer 初期化 -----------------------------------------
    Gst.init(None)

    source_desc = build_source_desc(args.input, args.latency)

    pipeline_desc = (
        f'{source_desc} ! '
        f'hailonet hef-path={args.hef_path} batch-size=1 '
        f'nms-score-threshold={args.score} nms-iou-threshold={args.iou} ! '
        f'hailofilter so-path={args.so_path} function-name={args.function_name} ! '
        'queue leaky=downstream max-size-buffers=3 ! '
        'identity name=identity_callback silent=true ! '
        'queue leaky=downstream max-size-buffers=3 ! '
        'hailooverlay ! videoconvert ! ' + args.sink
    )

    # パイプライン生成
    pipeline = Gst.parse_launch(pipeline_desc)

    # コールバック（hailofilter の出力を拾うため identity の src に差す）
    user_data = user_app_callback_class()
    identity = pipeline.get_by_name("identity_callback")
    if identity:
        pad = identity.get_static_pad("src")
        if pad:
            pad.add_probe(Gst.PadProbeType.BUFFER, app_callback, user_data)

    # 実行ループ
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_msg(bus, msg):
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"[Gst-ERROR] {err}; {dbg}")
            loop.quit()
        elif t == Gst.MessageType.EOS:
            loop.quit()
        return True

    bus.connect("message", on_msg)

    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    main()
