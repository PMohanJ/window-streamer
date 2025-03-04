# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# This file incorporates work covered by the following copyright and
# permission notice:
#
#   Copyright 2019 Google LLC
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import asyncio
import base64
import json
import logging
import os
import re
import sys
import time

logger = logging.getLogger("gstwebrtc_app")
logger.setLevel(logging.INFO)

try:
    import gi
    gi.require_version('GLib', "2.0")
    gi.require_version('Gst', "1.0")
    gi.require_version('GstRtp', "1.0")
    gi.require_version('GstSdp', "1.0")
    gi.require_version('GstWebRTC', "1.0")
    from gi.repository import GLib, Gst, GstRtp, GstSdp, GstWebRTC
    fract = Gst.Fraction(60, 1)
    del fract
except Exception as e:
    msg = """ERROR: could not find working GStreamer-Python installation.

If GStreamer is installed at a certain location, set the path to the environment variable GSTREAMER_PATH, then make sure your environment is set correctly using the below commands:

export GSTREAMER_PATH=${GSTREAMER_PATH:-/opt/gstreamer}
export PATH=${GSTREAMER_PATH}/bin${PATH:+:${PATH}}
export LD_LIBRARY_PATH=${GSTREAMER_PATH}/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}
export GI_TYPELIB_PATH=${GSTREAMER_PATH}/lib/x86_64-linux-gnu/girepository-1.0:/usr/lib/x86_64-linux-gnu/girepository-1.0${GI_TYPELIB_PATH:+:${GI_TYPELIB_PATH}}
export PYTHONPATH=${GSTREAMER_PATH}/lib/python3/dist-packages${PYTHONPATH:+:${PYTHONPATH}}

Replace x86_64-linux-gnu in other architectures manually or with "$(gcc -print-multiarch)".
"""
    logger.error(msg)
    logger.error(e)
    sys.exit(1)
logger.info("GStreamer-Python install looks OK")

class GSTWebRTCAppError(Exception):
    pass

class GSTWebRTCApp:
    def __init__(self, stun_servers=None, turn_servers=None, audio_channels=2, framerate=30, encoder=None, gpu_id=0, video_bitrate=2000, audio_bitrate=64000, keyframe_distance=3.0, congestion_control=False, video_packetloss_percent=0.0, audio_packetloss_percent=10.0, window=0):
        """Initialize GStreamer WebRTC app.

        Initializes GObjects and checks for required plugins.

        Arguments:
            stun_servers {[list of string]} -- Optional STUN server uris in the form of:
                                    stun:<host>:<port>
            turn_servers {[list of strings]} -- Optional TURN server uris in the form of:
                                    turn://<user>:<password>@<host>:<port>
        """

        self.stun_servers = stun_servers
        self.turn_servers = turn_servers
        self.audio_channels = audio_channels
        self.pipeline = None
        self.webrtcbin = None
        self.data_channel = None
        self.rtpgccbwe = None
        self.RTP_TWCC_URI = "http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01"
        self.congestion_control = congestion_control
        self.encoder = encoder
        self.gpu_id = gpu_id

        # default value is 0 - root window
        self.window_id_or_name = window

        self.framerate = framerate
        self.video_bitrate = video_bitrate
        self.audio_bitrate = audio_bitrate

        # Keyframe distance in seconds
        self.keyframe_distance = keyframe_distance
        # Packet loss base percentage
        self.video_packetloss_percent = video_packetloss_percent
        self.audio_packetloss_percent = audio_packetloss_percent
        # Prevent bitrate from overshooting because of FEC
        self.fec_video_bitrate = int(self.video_bitrate / (1.0 + (self.video_packetloss_percent / 100.0)))
        self.fec_audio_bitrate = int(self.audio_bitrate / (1.0 + (self.audio_packetloss_percent / 100.0)))

        # WebRTC ICE and SDP events
        self.on_ice = lambda mlineindex, candidate: logger.warn(
            'unhandled ice event')
        self.on_sdp = lambda sdp_type, sdp: logger.warn('unhandled sdp event')

        # Data channel events
        self.on_data_open = lambda: logger.warn('unhandled on_data_open')
        self.on_data_close = lambda: logger.warn('unhandled on_data_close')
        self.on_data_error = lambda: logger.warn('unhandled on_data_error')
        self.on_data_message = lambda msg: logger.warn(
            'unhandled on_data_message')

        Gst.init(None)

        self.check_plugins()

        self.ximagesrc = None
        self.ximagesrc_caps = None
        self.last_cursor_sent = None

    def stop_ximagesrc(self):
        """Helper function to stop the ximagesrc, useful when resizing
        """
        if self.ximagesrc:
            self.ximagesrc.set_state(Gst.State.NULL)

    def start_ximagesrc(self):
        """Helper function to start the ximagesrc, useful when resizing
        """
        if self.ximagesrc:
            self.ximagesrc.set_property("endx", 0)
            self.ximagesrc.set_property("endy", 0)
            self.ximagesrc.set_state(Gst.State.PLAYING)

    # [START build_webrtcbin_pipeline]
    def build_webrtcbin_pipeline(self, audio_only=False):
        """Adds the webrtcbin elements to the pipeline.

        The video and audio pipelines are linked to this in the
            build_video_pipeline() and build_audio_pipeline() methods.
        """
        # Reference configuration for webrtcbin including congestion control:
        #   https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs/-/blob/main/net/webrtc/src/webrtcsink/imp.rs

        # Create webrtcbin element named app
        self.webrtcbin = Gst.ElementFactory.make("webrtcbin", "app")

        # The bundle policy affects how the SDP is generated.
        # This will ultimately determine how many tracks the browser receives.
        # Setting this to max-compat will generate separate tracks for
        # audio and video.
        # See also: https://webrtcstandards.info/sdp-bundle/
        self.webrtcbin.set_property("bundle-policy", "max-compat")

        # Set default jitterbuffer latency to the minimum possible
        self.webrtcbin.set_property("latency", 1)

        # Connect signal handlers
        if self.congestion_control and not audio_only:
            self.webrtcbin.connect(
                'request-aux-sender', lambda webrtcbin, dtls_transport: self.__request_aux_sender(webrtcbin, dtls_transport))
        self.webrtcbin.connect(
            'on-negotiation-needed', lambda webrtcbin: self.__on_negotiation_needed(webrtcbin))
        self.webrtcbin.connect('on-ice-candidate', lambda webrtcbin, mlineindex,
                               candidate: self.__send_ice(webrtcbin, mlineindex, candidate))

        # Add STUN server
        # TODO: figure out how to add more than 1 stun server.
        if self.stun_servers:
            logger.info("updating STUN server")
            self.webrtcbin.set_property("stun-server", self.stun_servers[0])

        # Add TURN server
        if self.turn_servers:
            for i, turn_server in enumerate(self.turn_servers):
                logger.info("updating TURN server")
                if i == 0:
                    self.webrtcbin.set_property("turn-server", turn_server)
                else:
                    self.webrtcbin.emit("add-turn-server", turn_server)

        # Add element to the pipeline.
        self.pipeline.add(self.webrtcbin)
    # [END build_webrtcbin_pipeline]

    # [START build_video_pipeline]
    def build_video_old_pipeline(self):
        """Adds the RTP video stream to the pipeline.
        """

        # Create ximagesrc element named x11
        # Note that when using the ximagesrc plugin, ensure that the X11 server was
        # started with shared memory support: '+extension MIT-SHM' to achieve
        # full frame rates.
        # You can check if XSHM is in use with the following command:
        #   GST_DEBUG=default:5 gst-launch-1.0 ximagesrc ! fakesink num-buffers=1 2>&1 |grep -i xshm
        self.ximagesrc = Gst.ElementFactory.make("ximagesrc", "x11")
        ximagesrc = self.ximagesrc

        # disables display of the pointer using the XFixes extension,
        # common when building a remote desktop interface as the clients
        # mouse pointer can be used to give the user perceived lower latency.
        # This can be programmatically toggled after the pipeline is started
        # for example if the user is viewing fullscreen in the browser,
        # they may want to revert to seeing the remote cursor when the
        # client side cursor disappears.
        ximagesrc.set_property("show-pointer", 0)

        # Tells GStreamer that you are using an X11 window manager or
        # compositor with off-screen buffer. If you are not using a
        # window manager this can be set to 0. It's also important to
        # make sure that your X11 server is running with the XSHM extension
        # to ensure direct memory access to frames which will reduce latency.
        ximagesrc.set_property("remote", 1)

        # Defines the size in bytes to read per buffer. Increasing this from
        # the default of 4096 bytes helps performance when capturing high
        # resolutions like 1080P, and 2K.
        ximagesrc.set_property("blocksize", 16384)

        # The X11 XDamage extension allows the X server to indicate when a
        # regions of the screen has changed. While this can significantly
        # reduce CPU usage when the screen is idle, it has little effect with
        # constant motion. This can also have a negative consequences with H.264
        # as the video stream can drop out and take several seconds to recover
        # until a valid I-Frame is received.
        # Set this to 0 for most streaming use cases.
        ximagesrc.set_property("use-damage", 0)

        # Create capabilities for ximagesrc
        self.ximagesrc_caps = Gst.caps_from_string("video/x-raw")

        # Setting the framerate=60/1 capability instructs the ximagesrc element
        # to generate buffers at 60 frames per second (FPS).
        # The higher the FPS, the lower the latency so this parameter is one
        # way to set the overall target latency of the pipeline though keep in
        # mind that the pipeline may not always perfom at the full 60 FPS.
        self.ximagesrc_caps.set_value("framerate", Gst.Fraction(self.framerate, 1))

        # Create a capability filter for the ximagesrc_caps
        self.ximagesrc_capsfilter = Gst.ElementFactory.make("capsfilter")
        self.ximagesrc_capsfilter.set_property("caps", self.ximagesrc_caps)

        # ADD_ENCODER: add new encoder to this list
        # Reference configuration for fixing when something is broken in web browsers:
        #   https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs/-/blob/main/net/webrtc/src/webrtcsink/imp.rs
        if self.encoder in ["nvcudah264enc", "nvh264enc"]:
            # Upload buffers from ximagesrc directly to CUDA memory where
            # the colorspace conversion will be performed.
            cudaupload = Gst.ElementFactory.make("cudaupload")
            if self.gpu_id >= 0:
                cudaupload.set_property("cuda-device-id", self.gpu_id)

            # Convert the colorspace from BGRx to NVENC compatible format.
            # This is performed with CUDA which reduces the overall CPU load
            # compared to using the software videoconvert element.
            cudaconvert = Gst.ElementFactory.make("cudaconvert")
            if self.gpu_id >= 0:
                cudaconvert.set_property("cuda-device-id", self.gpu_id)

            # Convert ximagesrc BGRx format to NV12 using cudaconvert.
            # This is a more compatible format for client-side software decoders.
            cudaconvert_caps = Gst.caps_from_string("video/x-raw(memory:CUDAMemory)")
            cudaconvert_caps.set_value("format", "NV12")
            cudaconvert_capsfilter = Gst.ElementFactory.make("capsfilter")
            cudaconvert_capsfilter.set_property("caps", cudaconvert_caps)

            # Create the nvh264enc element named nvenc.
            # This is the heart of the video pipeline that converts the raw
            # frame buffers to an H.264 encoded byte-stream on the GPU.
            if self.gpu_id > 0:
                nvh264enc = Gst.ElementFactory.make("nvcudah264device{}enc".format(self.gpu_id), "nvenc")
                self.encoder = "nvcudah264enc"
                if nvh264enc is None:
                    nvh264enc = Gst.ElementFactory.make("nvh264device{}enc".format(self.gpu_id), "nvenc")
                    self.encoder = "nvh264enc"
            else:
                nvh264enc = Gst.ElementFactory.make("nvcudah264enc", "nvenc")
                self.encoder = "nvcudah264enc"
                if nvh264enc is None:
                    nvh264enc = Gst.ElementFactory.make("nvh264enc", "nvenc")
                    self.encoder = "nvh264enc"

            # The initial bitrate of the encoder in bits per second.
            # Setting this to 0 will use the bitrate from the NVENC preset.
            # This parameter can be set while the pipeline is running using the
            # set_video_bitrate() method. This helps to match the available
            # bandwidth. If set too high, the cliend side jitter buffer will
            # not be unable to lock on to the stream and it will fail to render.
            nvh264enc.set_property("bitrate", self.fec_video_bitrate)

            # Rate control mode tells the encoder how to compress the frames to
            # reach the target bitrate. A Constant Bit Rate (CBR) setting is best
            # for streaming use cases as bitrate is the most important factor.
            # A Variable Bit Rate (VBR) setting tells the encoder to adjust the
            # compression level based on scene complexity, something not needed
            # when streaming in real-time.
            if self.encoder != "nvh264enc":
                nvh264enc.set_property("rate-control", "cbr")
            else:
                nvh264enc.set_property("rc-mode", "cbr")

            # Group of Pictures (GOP) size is the distance between I-Frames that
            # contain the full frame data needed to render a whole frame.
            # A negative consequence when using infinite GOP size is that
            # when packets are lost, the decoder may never recover.
            # NVENC supports infinite GOP by setting this to -1.
            nvh264enc.set_property("gop-size", -1 if self.keyframe_distance == -1.0 else int(self.framerate * self.keyframe_distance))

            # Instructs encoder to handle Quality of Service (QOS) events from
            # the rest of the pipeline. Setting this to true increases
            # encoder stability.
            nvh264enc.set_property("qos", True)

            # The NVENC encoder supports a limited nubmer of encoding presets.
            # These presets are different than the open x264 standard.
            # The presets control the picture coding technique, bitrate,
            # and encoding quality.
            # low-latency-hq is the NVENC preset recommended for streaming.
            #
            # See this link for details on each preset:
            #   https://docs.nvidia.com/video-technologies/video-codec-sdk/12.2/nvenc-preset-migration-guide/index.html
            nvh264enc.set_property("aud", True)
            # Do not automatically add b-frames
            nvh264enc.set_property("b-adapt", False)
            # Automatic insertion of non-reference P-frames
            nvh264enc.set_property("nonref-p", True)
            # Disable lookahead
            nvh264enc.set_property("rc-lookahead", 0)
            if self.encoder != "nvh264enc":
                nvh264enc.set_property("b-frames", 0)
                # Insert sequence headers (SPS/PPS) per IDR
                nvh264enc.set_property("repeat-sequence-header", True)
                # Zero-latency operation mode (no reordering delay)
                nvh264enc.set_property("zero-reorder-delay", True)
                if Gst.version().major == 1 and Gst.version().minor <= 22:
                    nvh264enc.set_property("preset", "low-latency-hq")
                else:
                    nvh264enc.set_property("preset", "p4")
                    nvh264enc.set_property("tune", "ultra-low-latency")
            else:
                nvh264enc.set_property("bframes", 0)
                # Zero-latency operation mode (no reordering delay)
                nvh264enc.set_property("zerolatency", True)
                nvh264enc.set_property("preset", "low-latency-hq")

        elif self.encoder in ["nvcudah265enc", "nvh265enc"]:
            cudaupload = Gst.ElementFactory.make("cudaupload")
            if self.gpu_id >= 0:
                cudaupload.set_property("cuda-device-id", self.gpu_id)
            cudaconvert = Gst.ElementFactory.make("cudaconvert")
            if self.gpu_id >= 0:
                cudaconvert.set_property("cuda-device-id", self.gpu_id)
            cudaconvert_caps = Gst.caps_from_string("video/x-raw(memory:CUDAMemory)")
            cudaconvert_caps.set_value("format", "NV12")
            cudaconvert_capsfilter = Gst.ElementFactory.make("capsfilter")
            cudaconvert_capsfilter.set_property("caps", cudaconvert_caps)

            if self.gpu_id > 0:
                nvh265enc = Gst.ElementFactory.make("nvcudah265device{}enc".format(self.gpu_id), "nvenc")
                self.encoder = "nvcudah265enc"
                if nvh265enc is None:
                    nvh265enc = Gst.ElementFactory.make("nvh265device{}enc".format(self.gpu_id), "nvenc")
                    self.encoder = "nvh265enc"
            else:
                nvh265enc = Gst.ElementFactory.make("nvcudah265enc", "nvenc")
                self.encoder = "nvcudah265enc"
                if nvh265enc is None:
                    nvh265enc = Gst.ElementFactory.make("nvh265enc", "nvenc")
                    self.encoder = "nvh265enc"

            nvh265enc.set_property("bitrate", self.fec_video_bitrate)

            if self.encoder != "nvh265enc":
                nvh265enc.set_property("rate-control", "cbr")
            else:
                nvh265enc.set_property("rc-mode", "cbr")

            nvh265enc.set_property("gop-size", -1 if self.keyframe_distance == -1.0 else int(self.framerate * self.keyframe_distance))
            nvh265enc.set_property("qos", True)
            nvh265enc.set_property("aud", True)
            nvh265enc.set_property("b-adapt", False)
            nvh265enc.set_property("nonref-p", True)
            nvh265enc.set_property("rc-lookahead", 0)
            if self.encoder != "nvh265enc":
                nvh265enc.set_property("b-frames", 0)
                nvh265enc.set_property("repeat-sequence-header", True)
                nvh265enc.set_property("zero-reorder-delay", True)
                if Gst.version().major == 1 and Gst.version().minor <= 22:
                    nvh265enc.set_property("preset", "low-latency-hq")
                else:
                    nvh265enc.set_property("preset", "p4")
                    nvh265enc.set_property("tune", "ultra-low-latency")
            else:
                nvh265enc.set_property("bframes", 0)
                nvh265enc.set_property("zerolatency", True)
                nvh265enc.set_property("preset", "low-latency-hq")

        elif self.encoder in ["vah264enc", "vah264lpenc"]:
            # colorspace conversion
            if self.gpu_id > 0:
                vapostproc = Gst.ElementFactory.make("varenderD{}postproc".format(128 + self.gpu_id), "vapostproc")
            else:
                vapostproc = Gst.ElementFactory.make("vapostproc")
            vapostproc.set_property("scale-method", "fast")
            vapostproc_caps = Gst.caps_from_string("video/x-raw(memory:VAMemory)")
            vapostproc_caps.set_value("format", "NV12")
            vapostproc_capsfilter = Gst.ElementFactory.make("capsfilter")
            vapostproc_capsfilter.set_property("caps", vapostproc_caps)

            # encoder
            if self.gpu_id > 0:
                vah264enc = Gst.ElementFactory.make("varenderD{}h264enc".format(128 + self.gpu_id), "vaenc")
                self.encoder = "vah264enc"
                if vah264enc is None:
                    vah264enc = Gst.ElementFactory.make("varenderD{}h264lpenc".format(128 + self.gpu_id), "vaenc")
                    self.encoder = "vah264lpenc"
            else:
                vah264enc = Gst.ElementFactory.make("vah264enc", "vaenc")
                self.encoder = "vah264enc"
                if vah264enc is None:
                    vah264enc = Gst.ElementFactory.make("vah264lpenc", "vaenc")
                    self.encoder = "vah264lpenc"
            vah264enc.set_property("aud", True)
            vah264enc.set_property("b-frames", 0)
            vah264enc.set_property("key-int-max", 0 if self.keyframe_distance == -1.0 else int(self.framerate * self.keyframe_distance))
            vah264enc.set_property("rate-control", "cbr")
            vah264enc.set_property("target-usage", 6)
            vah264enc.set_property("qos", True)
            vah264enc.set_property("bitrate", self.fec_video_bitrate)

        elif self.encoder in ["vah265enc", "vah265lpenc"]:
            # colorspace conversion
            if self.gpu_id > 0:
                vapostproc = Gst.ElementFactory.make("varenderD{}postproc".format(128 + self.gpu_id), "vapostproc")
            else:
                vapostproc = Gst.ElementFactory.make("vapostproc")
            vapostproc.set_property("scale-method", "fast")
            vapostproc_caps = Gst.caps_from_string("video/x-raw(memory:VAMemory)")
            vapostproc_caps.set_value("format", "NV12")
            vapostproc_capsfilter = Gst.ElementFactory.make("capsfilter")
            vapostproc_capsfilter.set_property("caps", vapostproc_caps)

            # encoder
            if self.gpu_id > 0:
                vah265enc = Gst.ElementFactory.make("varenderD{}h265enc".format(128 + self.gpu_id), "vaenc")
                self.encoder = "vah265enc"
                if vah265enc is None:
                    vah265enc = Gst.ElementFactory.make("varenderD{}h265lpenc".format(128 + self.gpu_id), "vaenc")
                    self.encoder = "vah265lpenc"
            else:
                vah265enc = Gst.ElementFactory.make("vah265enc", "vaenc")
                self.encoder = "vah265enc"
                if vah265enc is None:
                    vah265enc = Gst.ElementFactory.make("vah265lpenc", "vaenc")
                    self.encoder = "vah265lpenc"
            vah265enc.set_property("aud", True)
            vah265enc.set_property("b-frames", 0)
            vah265enc.set_property("key-int-max", 0 if self.keyframe_distance == -1.0 else int(self.framerate * self.keyframe_distance))
            vah265enc.set_property("rate-control", "cbr")
            vah265enc.set_property("target-usage", 6)
            vah265enc.set_property("qos", True)
            vah265enc.set_property("bitrate", self.fec_video_bitrate)

        elif self.encoder in ["vavp9enc", "vavp9lpenc"]:
            # colorspace conversion
            if self.gpu_id > 0:
                vapostproc = Gst.ElementFactory.make("varenderD{}postproc".format(128 + self.gpu_id), "vapostproc")
            else:
                vapostproc = Gst.ElementFactory.make("vapostproc")
            vapostproc.set_property("scale-method", "fast")
            vapostproc_caps = Gst.caps_from_string("video/x-raw(memory:VAMemory)")
            vapostproc_caps.set_value("format", "NV12")
            vapostproc_capsfilter = Gst.ElementFactory.make("capsfilter")
            vapostproc_capsfilter.set_property("caps", vapostproc_caps)

            # encoder
            if self.gpu_id > 0:
                vavp9enc = Gst.ElementFactory.make("varenderD{}vp9enc".format(128 + self.gpu_id), "vaenc")
                self.encoder = "vavp9enc"
                if vavp9enc is None:
                    vavp9enc = Gst.ElementFactory.make("varenderD{}vp9lpenc".format(128 + self.gpu_id), "vaenc")
                    self.encoder = "vavp9lpenc"
            else:
                vavp9enc = Gst.ElementFactory.make("vavp9enc", "vaenc")
                self.encoder = "vavp9enc"
                if vavp9enc is None:
                    vavp9enc = Gst.ElementFactory.make("vavp9lpenc", "vaenc")
                    self.encoder = "vavp9lpenc"
            vavp9enc.set_property("key-int-max", 0 if self.keyframe_distance == -1.0 else int(self.framerate * self.keyframe_distance))
            vavp9enc.set_property("rate-control", "cbr")
            vavp9enc.set_property("target-usage", 6)
            vavp9enc.set_property("qos", True)
            vavp9enc.set_property("bitrate", self.fec_video_bitrate)

        elif self.encoder in ["vaav1enc", "vaav1lpenc"]:
            # colorspace conversion
            if self.gpu_id > 0:
                vapostproc = Gst.ElementFactory.make("varenderD{}postproc".format(128 + self.gpu_id), "vapostproc")
            else:
                vapostproc = Gst.ElementFactory.make("vapostproc")
            vapostproc.set_property("scale-method", "fast")
            vapostproc_caps = Gst.caps_from_string("video/x-raw(memory:VAMemory)")
            vapostproc_caps.set_value("format", "NV12")
            vapostproc_capsfilter = Gst.ElementFactory.make("capsfilter")
            vapostproc_capsfilter.set_property("caps", vapostproc_caps)

            # encoder
            if self.gpu_id > 0:
                vaav1enc = Gst.ElementFactory.make("varenderD{}av1enc".format(128 + self.gpu_id), "vaenc")
                self.encoder = "vaav1enc"
                if vaav1enc is None:
                    vaav1enc = Gst.ElementFactory.make("varenderD{}av1lpenc".format(128 + self.gpu_id), "vaenc")
                    self.encoder = "vaav1lpenc"
            else:
                vaav1enc = Gst.ElementFactory.make("vaav1enc", "vaenc")
                self.encoder = "vaav1enc"
                if vaav1enc is None:
                    vaav1enc = Gst.ElementFactory.make("vaav1lpenc", "vaenc")
                    self.encoder = "vaav1lpenc"
            vaav1enc.set_property("key-int-max", 0 if self.keyframe_distance == -1.0 else int(self.framerate * self.keyframe_distance))
            vaav1enc.set_property("rate-control", "cbr")
            vaav1enc.set_property("target-usage", 6)
            vaav1enc.set_property("qos", True)
            vaav1enc.set_property("bitrate", self.fec_video_bitrate)

        elif self.encoder in ["x264enc"]:
            # Videoconvert for colorspace conversion
            videoconvert = Gst.ElementFactory.make("videoconvert")
            videoconvert.set_property("n-threads", min(4, max(1, len(os.sched_getaffinity(0)) - 1)))
            videoconvert_caps = Gst.caps_from_string("video/x-raw")
            videoconvert_caps.set_value("format", "NV12")
            videoconvert_capsfilter = Gst.ElementFactory.make("capsfilter")
            videoconvert_capsfilter.set_property("caps", videoconvert_caps)

            # encoder
            x264enc = Gst.ElementFactory.make("x264enc", "x264enc")
            x264enc.set_property("threads", min(8, max(1, len(os.sched_getaffinity(0)) - 1)))
            x264enc.set_property("aud", True)
            x264enc.set_property("b-adapt", False)
            x264enc.set_property("bframes", 0)
            x264enc.set_property("insert-vui", True)
            x264enc.set_property("key-int-max", 2147483647 if self.keyframe_distance == -1.0 else int(self.framerate * self.keyframe_distance))
            x264enc.set_property("rc-lookahead", 0)
            x264enc.set_property("vbv-buf-capacity", 120)
            x264enc.set_property("sliced-threads", True)
            x264enc.set_property("byte-stream", True)
            x264enc.set_property("pass", "cbr")
            x264enc.set_property("speed-preset", "veryfast")
            x264enc.set_property("tune", "zerolatency")
            x264enc.set_property("qos", True)
            x264enc.set_property("bitrate", self.fec_video_bitrate)

        elif self.encoder in ["openh264enc"]:
            # Videoconvert for colorspace conversion
            videoconvert = Gst.ElementFactory.make("videoconvert")
            videoconvert.set_property("n-threads", min(4, max(1, len(os.sched_getaffinity(0)) - 1)))
            videoconvert_caps = Gst.caps_from_string("video/x-raw")
            videoconvert_caps.set_value("format", "I420")
            videoconvert_capsfilter = Gst.ElementFactory.make("capsfilter")
            videoconvert_capsfilter.set_property("caps", videoconvert_caps)

            # encoder
            openh264enc = Gst.ElementFactory.make("openh264enc", "openh264enc")
            openh264enc.set_property("adaptive-quantization", False)
            openh264enc.set_property("background-detection", False)
            openh264enc.set_property("enable-frame-skip", False)
            openh264enc.set_property("scene-change-detection", False)
            openh264enc.set_property("usage-type", "screen")
            openh264enc.set_property("complexity", "low")
            openh264enc.set_property("gop-size", 2147483647 if self.keyframe_distance == -1.0 else int(self.framerate * self.keyframe_distance))
            openh264enc.set_property("multi-thread", min(8, max(1, len(os.sched_getaffinity(0)) - 1)))
            openh264enc.set_property("slice-mode", "n-slices")
            openh264enc.set_property("num-slices", min(4, max(1, len(os.sched_getaffinity(0)) - 1)))
            openh264enc.set_property("rate-control", "bitrate")
            openh264enc.set_property("bitrate", self.fec_video_bitrate * 1000)

        elif self.encoder in ["x265enc"]:
            # Videoconvert for colorspace conversion
            videoconvert = Gst.ElementFactory.make("videoconvert")
            videoconvert.set_property("n-threads", min(4, max(1, len(os.sched_getaffinity(0)) - 1)))
            videoconvert_caps = Gst.caps_from_string("video/x-raw")
            videoconvert_caps.set_value("format", "I420")
            videoconvert_capsfilter = Gst.ElementFactory.make("capsfilter")
            videoconvert_capsfilter.set_property("caps", videoconvert_caps)

            # encoder
            x265enc = Gst.ElementFactory.make("x265enc", "x265enc")
            x265enc.set_property("option-string", "b-adapt=0:bframes=0:rc-lookahead=0:aud:repeat-headers:pmode:wpp")
            x265enc.set_property("key-int-max", 2147483647 if self.keyframe_distance == -1.0 else int(self.framerate * self.keyframe_distance))
            x265enc.set_property("qos", True)
            x265enc.set_property("speed-preset", "ultrafast")
            x265enc.set_property("tune", "zerolatency")
            x265enc.set_property("bitrate", self.fec_video_bitrate)

        elif self.encoder in ["vp8enc", "vp9enc"]:
            videoconvert = Gst.ElementFactory.make("videoconvert")
            videoconvert.set_property("n-threads", min(4, max(1, len(os.sched_getaffinity(0)) - 1)))
            videoconvert_caps = Gst.caps_from_string("video/x-raw")
            videoconvert_caps.set_value("format", "I420")
            videoconvert_capsfilter = Gst.ElementFactory.make("capsfilter")
            videoconvert_capsfilter.set_property("caps", videoconvert_caps)

            if self.encoder == "vp8enc":
                vpenc = Gst.ElementFactory.make("vp8enc", "vpenc")

            elif self.encoder == "vp9enc":
                vpenc = Gst.ElementFactory.make("vp9enc", "vpenc")
                vpenc.set_property("frame-parallel-decoding", True)
                vpenc.set_property("row-mt", True)

            # VPX Parameters
            vpenc.set_property("threads", min(8, max(1, len(os.sched_getaffinity(0)) - 1)))
            vpenc.set_property("buffer-initial-size", 100)
            vpenc.set_property("buffer-optimal-size", 120)
            vpenc.set_property("buffer-size", 150)
            vpenc.set_property("max-intra-bitrate", 250)
            vpenc.set_property("cpu-used", -16)
            vpenc.set_property("deadline", 1)
            vpenc.set_property("end-usage", "cbr")
            vpenc.set_property("error-resilient", "default")
            vpenc.set_property("keyframe-mode", "disabled")
            vpenc.set_property("keyframe-max-dist", 2147483647 if self.keyframe_distance == -1.0 else int(self.framerate * self.keyframe_distance))
            vpenc.set_property("lag-in-frames", 0)
            vpenc.set_property("qos", True)
            vpenc.set_property("target-bitrate", self.fec_video_bitrate * 1000)

        elif self.encoder in ["rav1enc"]:
            videoconvert = Gst.ElementFactory.make("videoconvert")
            videoconvert.set_property("n-threads", min(4, max(1, len(os.sched_getaffinity(0)) - 1)))
            videoconvert_caps = Gst.caps_from_string("video/x-raw")
            videoconvert_caps.set_value("format", "I420")
            videoconvert_capsfilter = Gst.ElementFactory.make("capsfilter")
            videoconvert_capsfilter.set_property("caps", videoconvert_caps)

            rav1enc = Gst.ElementFactory.make("rav1enc", "rav1enc")
            rav1enc.set_property("low-latency", True)
            rav1enc.set_property("max-key-frame-interval", 715827882 if self.keyframe_distance == -1.0 else int(self.framerate * self.keyframe_distance))
            rav1enc.set_property("rdo-lookahead-frames", 0)
            rav1enc.set_property("speed-preset", 10)
            rav1enc.set_property("tiles", 16)
            rav1enc.set_property("threads", min(16, max(1, len(os.sched_getaffinity(0)) - 1)))
            rav1enc.set_property("qos", True)
            rav1enc.set_property("bitrate", self.fec_video_bitrate)

        else:
            raise GSTWebRTCAppError("Unsupported encoder for pipeline: %s" % self.encoder)

        if "h264" in self.encoder or "x264" in self.encoder:
            # Set the capabilities for the H.264 codec.
            h264enc_caps = Gst.caps_from_string("video/x-h264")

            # Sets the H.264 encoding profile to one compatible with WebRTC.
            # The high profile is used for streaming HD video.
            # Browsers only support specific H.264 profiles and they are
            # coded in the RTP payload type set by the rtph264pay_caps below.
            h264enc_caps.set_value("profile", "high")

            # Stream-oriented H.264 codec
            h264enc_caps.set_value("stream-format", "byte-stream")

            # Create a capability filter for the h264enc_caps.
            h264enc_capsfilter = Gst.ElementFactory.make("capsfilter")
            h264enc_capsfilter.set_property("caps", h264enc_caps)

            # Create the rtph264pay element to convert buffers into
            # RTP packets that are sent over the connection transport.
            rtph264pay = Gst.ElementFactory.make("rtph264pay")
            rtph264pay.set_property("mtu", 1200)

            # Default aggregate mode for WebRTC
            rtph264pay.set_property("aggregate-mode", "zero-latency")

            # Send SPS and PPS Insertion with every IDR frame
            rtph264pay.set_property("config-interval", -1)

            # Add Transport-Wide Congestion Control (TWCC) extension
            twcc_id_video = self.__pick_twcc_extension_id(rtph264pay)
            if twcc_id_video is not None:
                twcc_extension_video = GstRtp.RTPHeaderExtension.create_from_uri(self.RTP_TWCC_URI)
                twcc_extension_video.set_id(twcc_id_video)
                rtph264pay.emit("add-extension", twcc_extension_video)

            # Set the capabilities for the rtph264pay element.
            rtph264pay_caps = Gst.caps_from_string("application/x-rtp")

            # Set the payload type to video.
            rtph264pay_caps.set_value("media", "video")
            rtph264pay_caps.set_value("clock-rate", 90000)

            # Set the video encoding name to match our encoded format.
            rtph264pay_caps.set_value("encoding-name", "H264")

            # Set the payload type to one that matches the encoding profile.
            # Fake to the constrained-baseline profile for Firefox
            # High profile will still be decoded
            # Other payloads can be derived using WebRTC specification:
            #   https://tools.ietf.org/html/rfc6184#section-8.2.1
            rtph264pay_caps.set_value("payload", 97)

            # Set caps that help with frame retransmits that will avoid screen freezing on packet loss.
            rtph264pay_caps.set_value("rtcp-fb-nack-pli", True)
            rtph264pay_caps.set_value("rtcp-fb-ccm-fir", True)
            rtph264pay_caps.set_value("rtcp-fb-x-gstreamer-fir-as-repair", True)

            # Create a capability filter for the rtph264pay_caps.
            rtph264pay_capsfilter = Gst.ElementFactory.make("capsfilter")
            rtph264pay_capsfilter.set_property("caps", rtph264pay_caps)

        elif "h265" in self.encoder or "x265" in self.encoder:
            h265enc_caps = Gst.caps_from_string("video/x-h265")
            h265enc_caps.set_value("profile", "main")
            h265enc_caps.set_value("stream-format", "byte-stream")
            h265enc_capsfilter = Gst.ElementFactory.make("capsfilter")
            h265enc_capsfilter.set_property("caps", h265enc_caps)

            rtph265pay = Gst.ElementFactory.make("rtph265pay")
            rtph265pay.set_property("mtu", 1200)
            rtph265pay.set_property("aggregate-mode", "zero-latency")
            rtph265pay.set_property("config-interval", -1)
            twcc_id_video = self.__pick_twcc_extension_id(rtph265pay)
            if twcc_id_video is not None:
                twcc_extension_video = GstRtp.RTPHeaderExtension.create_from_uri(self.RTP_TWCC_URI)
                twcc_extension_video.set_id(twcc_id_video)
                rtph265pay.emit("add-extension", twcc_extension_video)
            rtph265pay_caps = Gst.caps_from_string("application/x-rtp")
            rtph265pay_caps.set_value("media", "video")
            rtph265pay_caps.set_value("clock-rate", 90000)
            rtph265pay_caps.set_value("encoding-name", "H265")
            rtph265pay_caps.set_value("payload", 100)
            rtph265pay_caps.set_value("rtcp-fb-nack-pli", True)
            rtph265pay_caps.set_value("rtcp-fb-ccm-fir", True)
            rtph265pay_caps.set_value("rtcp-fb-x-gstreamer-fir-as-repair", True)
            rtph265pay_capsfilter = Gst.ElementFactory.make("capsfilter")
            rtph265pay_capsfilter.set_property("caps", rtph265pay_caps)

        elif "vp8" in self.encoder:
            vpenc_caps = Gst.caps_from_string("video/x-vp8")
            vpenc_capsfilter = Gst.ElementFactory.make("capsfilter")
            vpenc_capsfilter.set_property("caps", vpenc_caps)

            rtpvppay = Gst.ElementFactory.make("rtpvp8pay", "rtpvppay")
            rtpvppay.set_property("mtu", 1200)
            rtpvppay.set_property("picture-id-mode", "15-bit")
            twcc_id_video = self.__pick_twcc_extension_id(rtpvppay)
            if twcc_id_video is not None:
                twcc_extension_video = GstRtp.RTPHeaderExtension.create_from_uri(self.RTP_TWCC_URI)
                twcc_extension_video.set_id(twcc_id_video)
                rtpvppay.emit("add-extension", twcc_extension_video)
            rtpvppay_caps = Gst.caps_from_string("application/x-rtp")
            rtpvppay_caps.set_value("media", "video")
            rtpvppay_caps.set_value("clock-rate", 90000)
            rtpvppay_caps.set_value("encoding-name", "VP8")
            rtpvppay_caps.set_value("payload", 96)
            rtpvppay_caps.set_value("rtcp-fb-nack-pli", True)
            rtpvppay_caps.set_value("rtcp-fb-ccm-fir", True)
            rtpvppay_caps.set_value("rtcp-fb-x-gstreamer-fir-as-repair", True)
            rtpvppay_capsfilter = Gst.ElementFactory.make("capsfilter")
            rtpvppay_capsfilter.set_property("caps", rtpvppay_caps)

        elif "vp9" in self.encoder:
            vpenc_caps = Gst.caps_from_string("video/x-vp9")
            vpenc_capsfilter = Gst.ElementFactory.make("capsfilter")
            vpenc_capsfilter.set_property("caps", vpenc_caps)

            rtpvppay = Gst.ElementFactory.make("rtpvp9pay", "rtpvppay")
            rtpvppay.set_property("mtu", 1200)
            rtpvppay.set_property("picture-id-mode", "15-bit")
            twcc_id_video = self.__pick_twcc_extension_id(rtpvppay)
            if twcc_id_video is not None:
                twcc_extension_video = GstRtp.RTPHeaderExtension.create_from_uri(self.RTP_TWCC_URI)
                twcc_extension_video.set_id(twcc_id_video)
                rtpvppay.emit("add-extension", twcc_extension_video)
            rtpvppay_caps = Gst.caps_from_string("application/x-rtp")
            rtpvppay_caps.set_value("media", "video")
            rtpvppay_caps.set_value("clock-rate", 90000)
            rtpvppay_caps.set_value("encoding-name", "VP9")
            rtpvppay_caps.set_value("payload", 98)
            rtpvppay_caps.set_value("rtcp-fb-nack-pli", True)
            rtpvppay_caps.set_value("rtcp-fb-ccm-fir", True)
            rtpvppay_caps.set_value("rtcp-fb-x-gstreamer-fir-as-repair", True)
            rtpvppay_capsfilter = Gst.ElementFactory.make("capsfilter")
            rtpvppay_capsfilter.set_property("caps", rtpvppay_caps)

        elif "av1" in self.encoder:
            av1enc_caps = Gst.caps_from_string("video/x-av1")
            av1enc_caps.set_value("parsed", True)
            av1enc_caps.set_value("stream-format", "obu-stream")
            av1enc_capsfilter = Gst.ElementFactory.make("capsfilter")
            av1enc_capsfilter.set_property("caps", av1enc_caps)

            rtpav1pay = Gst.ElementFactory.make("rtpav1pay")
            rtpav1pay.set_property("mtu", 1200)
            twcc_id_video = self.__pick_twcc_extension_id(rtpav1pay)
            if twcc_id_video is not None:
                twcc_extension_video = GstRtp.RTPHeaderExtension.create_from_uri(self.RTP_TWCC_URI)
                twcc_extension_video.set_id(twcc_id_video)
                rtpav1pay.emit("add-extension", twcc_extension_video)
            rtpav1pay_caps = Gst.caps_from_string("application/x-rtp")
            rtpav1pay_caps.set_value("media", "video")
            rtpav1pay_caps.set_value("clock-rate", 90000)
            rtpav1pay_caps.set_value("encoding-name", "AV1")
            rtpav1pay_caps.set_value("payload", 99)
            rtpav1pay_caps.set_value("rtcp-fb-nack-pli", True)
            rtpav1pay_caps.set_value("rtcp-fb-ccm-fir", True)
            rtpav1pay_caps.set_value("rtcp-fb-x-gstreamer-fir-as-repair", True)
            rtpav1pay_capsfilter = Gst.ElementFactory.make("capsfilter")
            rtpav1pay_capsfilter.set_property("caps", rtpav1pay_caps)

        # Add all elements to the pipeline.
        pipeline_elements = [self.ximagesrc, self.ximagesrc_capsfilter]

        # ADD_ENCODER: add new encoder to this list
        if self.encoder in ["nvcudah264enc", "nvh264enc"]:
            pipeline_elements += [cudaupload, cudaconvert, cudaconvert_capsfilter, nvh264enc, h264enc_capsfilter, rtph264pay, rtph264pay_capsfilter]

        elif self.encoder in ["nvcudah265enc", "nvh265enc"]:
            pipeline_elements += [cudaupload, cudaconvert, cudaconvert_capsfilter, nvh265enc, h265enc_capsfilter, rtph265pay, rtph265pay_capsfilter]

        elif self.encoder in ["vah264enc", "vah264lpenc"]:
            pipeline_elements += [vapostproc, vapostproc_capsfilter, vah264enc, h264enc_capsfilter, rtph264pay, rtph264pay_capsfilter]

        elif self.encoder in ["vah265enc", "vah265lpenc"]:
            pipeline_elements += [vapostproc, vapostproc_capsfilter, vah265enc, h265enc_capsfilter, rtph265pay, rtph265pay_capsfilter]

        elif self.encoder in ["vavp9enc", "vavp9lpenc"]:
            pipeline_elements += [vapostproc, vapostproc_capsfilter, vavp9enc, vpenc_capsfilter, rtpvppay, rtpvppay_capsfilter]

        elif self.encoder in ["vaav1enc", "vaav1lpenc"]:
            pipeline_elements += [vapostproc, vapostproc_capsfilter, vaav1enc, av1enc_capsfilter, rtpav1pay, rtpav1pay_capsfilter]

        elif self.encoder in ["x264enc"]:
            pipeline_elements += [videoconvert, videoconvert_capsfilter, x264enc, h264enc_capsfilter, rtph264pay, rtph264pay_capsfilter]

        elif self.encoder in ["openh264enc"]:
            pipeline_elements += [videoconvert, videoconvert_capsfilter, openh264enc, h264enc_capsfilter, rtph264pay, rtph264pay_capsfilter]

        elif self.encoder in ["x265enc"]:
            pipeline_elements += [videoconvert, videoconvert_capsfilter, x265enc, h265enc_capsfilter, rtph265pay, rtph265pay_capsfilter]

        elif self.encoder in ["vp8enc", "vp9enc"]:
            pipeline_elements += [videoconvert, videoconvert_capsfilter, vpenc, vpenc_capsfilter, rtpvppay, rtpvppay_capsfilter]

        elif self.encoder in ["rav1enc"]:
            pipeline_elements += [videoconvert, videoconvert_capsfilter, rav1enc, av1enc_capsfilter, rtpav1pay, rtpav1pay_capsfilter]

        for pipeline_element in pipeline_elements:
            self.pipeline.add(pipeline_element)

        # Link the pipeline elements and raise exception of linking fails
        # due to incompatible element pad capabilities.
        pipeline_elements += [self.webrtcbin]
        for i in range(len(pipeline_elements) - 1):
            if not Gst.Element.link(pipeline_elements[i], pipeline_elements[i + 1]):
                raise GSTWebRTCAppError("Failed to link {} -> {}".format(pipeline_elements[i].get_name(), pipeline_elements[i + 1].get_name()))

        # Enable NACKs on the transceiver with video streams, helps with retransmissions and freezing when packets are dropped.
        transceiver = self.webrtcbin.emit("get-transceiver", 0)
        transceiver.set_property("do-nack", True)
        transceiver.set_property("fec-type", GstWebRTC.WebRTCFECType.ULP_RED if self.video_packetloss_percent > 0 else GstWebRTC.WebRTCFECType.NONE)
        transceiver.set_property("fec-percentage", self.video_packetloss_percent)
    # [END build_video_pipeline]

    def build_video_pipeline(self):
        """Adds the RTP video stream to the pipeline.
        """

        # Create ximagesrc element named x11
        # Note that when using the ximagesrc plugin, ensure that the X11 server was
        # started with shared memory support: '+extension MIT-SHM' to achieve
        # full frame rates.
        # You can check if XSHM is in use with the following command:
        #   GST_DEBUG=default:5 gst-launch-1.0 ximagesrc ! fakesink num-buffers=1 2>&1 |grep -i xshm
        self.ximagesrc = Gst.ElementFactory.make("ximagesrc", "x11")
        ximagesrc = self.ximagesrc

        if self.window_id_or_name:
            ximagesrc.set_property("XID", self.window_id_or_name)
        # disables display of the pointer using the XFixes extension,
        # common when building a remote desktop interface as the clients
        # mouse pointer can be used to give the user perceived lower latency.
        # This can be programmatically toggled after the pipeline is started
        # for example if the user is viewing full screen in the browser,
        # they may want to revert to seeing the remote cursor when the
        # # client side cursor disappears.
        # ximagesrc.set_property("show-pointer", 0)

        # Tells GStreamer that you are using an X11 window manager or
        # compositor with off-screen buffer. If you are not using a
        # window manager this can be set to 0. It's also important to
        # make sure that your X11 server is running with the XSHM extension
        # to ensure direct memory access to frames which will reduce latency.
        #ximagesrc.set_property("remote", 1)

        # Defines the size in bytes to read per buffer. Increasing this from
        # the default of 4096 bytes helps performance when capturing high
        # resolutions like 1080P, and 2K.
        ximagesrc.set_property("blocksize", 16384)

        # The X11 XDamage extension allows the X server to indicate when a
        # regions of the screen has changed. While this can significantly
        # reduce CPU usage when the screen is idle, it has little effect with
        # constant motion. This can also have a negative consequences with H.264
        # as the video stream can drop out and take several seconds to recover
        # until a valid I-Frame is received.
        # Set this to 0 for most streaming use cases.
        ximagesrc.set_property("use-damage", 0)

        # Create capabilities for ximagesrc
        ximagesrc_caps = Gst.caps_from_string("video/x-raw")

        # Setting the framerate=60/1 capability instructs the ximagesrc element
        # to generate buffers at 60 frames per second (FPS).
        # The higher the FPS, the lower the latency so this parameter is one
        # way to set the overall target latency of the pipeline though keep in
        # mind that the pipeline may not always perfom at the full 60 FPS.
        ximagesrc_caps.set_value("framerate", Gst.Fraction(self.framerate, 1))

        # Create a capability filter for the ximagesrc_caps
        ximagesrc_capsfilter = Gst.ElementFactory.make("capsfilter")
        ximagesrc_capsfilter.set_property("caps", ximagesrc_caps)

        if self.encoder in ["nvh264enc"]:
            # Upload buffers from ximagesrc directly to CUDA memory where
            # the colorspace conversion will be performed.
            cudaupload = Gst.ElementFactory.make("cudaupload")

            # Convert the colorspace from BGRx to NVENC compatible format.
            # This is performed with CUDA which reduces the overall CPU load
            # compared to using the software videoconvert element.
            cudaconvert = Gst.ElementFactory.make("cudaconvert")

            # Convert ximagesrc BGRx format to I420 using cudaconvert.
            # This is a more compatible format for client-side software decoders.
            cudaconvert_caps = Gst.caps_from_string("video/x-raw(memory:CUDAMemory)")
            cudaconvert_caps.set_value("format", "I420")
            cudaconvert_capsfilter = Gst.ElementFactory.make("capsfilter")
            cudaconvert_capsfilter.set_property("caps", cudaconvert_caps)

            # Create the nvh264enc element named nvenc.
            # This is the heart of the video pipeline that converts the raw
            # frame buffers to an H.264 encoded byte-stream on the GPU.
            nvh264enc = Gst.ElementFactory.make("nvh264enc", "nvenc")

            # The initial bitrate of the encoder in bits per second.
            # Setting this to 0 will use the bitrate from the NVENC preset.
            # This parameter can be set while the pipeline is running using the
            # set_video_bitrate() method. This helps to match the available
            # bandwidth. If set too high, the cliend side jitter buffer will
            # not be unable to lock on to the stream and it will fail to render.
            nvh264enc.set_property("bitrate", self.video_bitrate)

            # Rate control mode tells the encoder how to compress the frames to
            # reach the target bitrate. A Constant Bit Rate (CBR) setting is best
            # for streaming use cases as bit rate is the most important factor.
            # A Variable Bit Rate (VBR) setting tells the encoder to adjust the
            # compression level based on scene complexity, something not needed
            # when streaming in real-time.
            nvh264enc.set_property("rc-mode", "cbr")

            # Group of Pictures (GOP) size is the distance between I-Frames that
            # contain the full frame data needed to render a whole frame.
            # Infinite GOP is best for streaming because it reduces the number
            # of large I-Frames being transmitted. At higher resolutions, these
            # I-Frames can dominate the bandwidth and add additional latency.
            # With infinite GOP, you can use a higher bit rate to increase quality
            # without a linear increase in total bandwidth.
            # A negative consequence when using infinite GOP size is that
            # when packets are lost, it may take the decoder longer to recover.
            # NVENC supports infinite GOP by setting this to -1.
            nvh264enc.set_property("gop-size", -1)

            # Instructs encoder to handle Quality of Service (QOS) events from
            # the rest of the pipeline. Setting this to true increases
            # encoder stability.
            nvh264enc.set_property("qos", True)

            # The NVENC encoder supports a limited nubmer of encoding presets.
            # These presets are different than the open x264 standard.
            # The presets control the picture coding technique, bitrate,
            # and encoding quality.
            # The low-latency-hq is the NVENC preset reccomended for streaming.
            #
            # See this link for details on each preset:
            #   https://streamquality.report/docs/report.html#1080p60-nvenc-h264-picture-quality
            nvh264enc.set_property("preset", "low-latency-hq")

            # Set the capabilities for the nvh264enc element.
            nvh264enc_caps = Gst.caps_from_string("video/x-h264")

            # Sets the H.264 encoding profile to one compatible with WebRTC.
            # The high profile is used for streaming HD video.
            # Browsers only support specific H.264 profiles and they are
            # coded in the RTP payload type set by the rtph264pay_caps below.
            nvh264enc_caps.set_value("profile", "high")

            # Create a capability filter for the nvh264enc_caps.
            nvh264enc_capsfilter = Gst.ElementFactory.make("capsfilter")
            nvh264enc_capsfilter.set_property("caps", nvh264enc_caps)

            # Create the rtph264pay element to convert buffers into
            # RTP packets that are sent over the connection transport.
            rtph264pay = Gst.ElementFactory.make("rtph264pay")

            # Set the capabilities for the rtph264pay element.
            rtph264pay_caps = Gst.caps_from_string("application/x-rtp")

            # Set the payload type to video.
            rtph264pay_caps.set_value("media", "video")

            # Set the video encoding name to match our encoded format.
            rtph264pay_caps.set_value("encoding-name", "H264")

            # Set the payload type to one that matches the encoding profile.
            # Payload number 123 corresponds to H.264 encoding with the high profile.
            # Other payloads can be derived using WebRTC specification:
            #   https://tools.ietf.org/html/rfc6184#section-8.2.1
            rtph264pay_caps.set_value("payload", 123)

            # Set caps that help with frame retransmits that will avoid screen freezing on packet loss.
            rtph264pay_caps.set_value("rtcp-fb-nack-pli", True)
            rtph264pay_caps.set_value("rtcp-fb-ccm-fir", True)
            rtph264pay_caps.set_value("rtcp-fb-x-gstreamer-fir-as-repair", True)

            # Set aggregate-mode to reduce RTP packetization overhead
            rtph264pay_caps.set_value("aggregate-mode", "zero-latency")

            # Create a capability filter for the rtph264pay_caps.
            rtph264pay_capsfilter = Gst.ElementFactory.make("capsfilter")
            rtph264pay_capsfilter.set_property("caps", rtph264pay_caps)

        elif self.encoder in ["x264enc"]:
            # Videoconvert for colorspace conversion
            videoconvert = Gst.ElementFactory.make("videoconvert")
            videoconvert_caps = Gst.caps_from_string("video/x-raw")
            videoconvert_capsfilter = Gst.ElementFactory.make("capsfilter")
            videoconvert_capsfilter.set_property("caps", videoconvert_caps)

            # encoder
            x264enc = Gst.ElementFactory.make("x264enc", "x264enc")
            x264enc.set_property("threads", 4)
            x264enc.set_property("bframes", 0)
            x264enc.set_property("key-int-max", 0)
            x264enc.set_property("byte-stream", True)
            x264enc.set_property("tune", "zerolatency")
            x264enc.set_property("speed-preset", "veryfast")
            x264enc.set_property("bitrate", self.video_bitrate)

            # capsfilter
            x264enc_caps = Gst.caps_from_string("video/x-h264")
            x264enc_caps.set_value("stream-format", "byte-stream")
            x264enc_caps.set_value("profile", "high")
            x264enc_capsfilter = Gst.ElementFactory.make("capsfilter")
            x264enc_capsfilter.set_property("caps", x264enc_caps)

            # RTP payload
            rtph264pay = Gst.ElementFactory.make("rtph264pay")
            rtph264pay_caps = Gst.caps_from_string("application/x-rtp")
            rtph264pay_caps.set_value("media", "video")
            rtph264pay_caps.set_value("encoding-name", "H264")
            rtph264pay_caps.set_value("payload", 123)
            rtph264pay_caps.set_value("rtcp-fb-nack-pli", True)
            rtph264pay_caps.set_value("rtcp-fb-ccm-fir", True)
            rtph264pay_caps.set_value("rtcp-fb-x-gstreamer-fir-as-repair", True)
            rtph264pay_caps.set_value("aggregate-mode", "zero-latency")

            # Create a capability filter for the rtph264pay_caps.
            rtph264pay_capsfilter = Gst.ElementFactory.make("capsfilter")
            rtph264pay_capsfilter.set_property("caps", rtph264pay_caps)

        elif self.encoder in ["vp8enc", "vp9enc"]:
            videoconvert = Gst.ElementFactory.make("videoconvert")
            videoconvert_caps = Gst.caps_from_string("video/x-raw,format=I420")
            videoconvert_capsfilter = Gst.ElementFactory.make("capsfilter")
            videoconvert_capsfilter.set_property("caps", videoconvert_caps)

            if self.encoder == "vp8enc":
                vpenc = Gst.ElementFactory.make("vp8enc", "vpenc")
                vpenc_caps = Gst.caps_from_string("video/x-vp8")
                vpenc_capsfilter = Gst.ElementFactory.make("capsfilter")
                vpenc_capsfilter.set_property("caps", vpenc_caps)

                rtpvppay = Gst.ElementFactory.make("rtpvp8pay")
                rtpvppay_caps = Gst.caps_from_string("application/x-rtp")
                rtpvppay_caps.set_value("media", "video")
                rtpvppay_caps.set_value("encoding-name", "VP8")
                rtpvppay_caps.set_value("payload", 123)
                rtpvppay_capsfilter = Gst.ElementFactory.make("capsfilter")
                rtpvppay_capsfilter.set_property("caps", rtpvppay_caps)

            if self.encoder == "vp9enc":
                vpenc = Gst.ElementFactory.make("vp9enc", "vpenc")
                vpenc_caps = Gst.caps_from_string("video/x-vp9")
                vpenc_capsfilter = Gst.ElementFactory.make("capsfilter")
                vpenc_capsfilter.set_property("caps", vpenc_caps)

                rtpvppay = Gst.ElementFactory.make("rtpvp9pay")
                rtpvppay_caps = Gst.caps_from_string("application/x-rtp")
                rtpvppay_caps.set_value("media", "video")
                rtpvppay_caps.set_value("encoding-name", "VP9")
                rtpvppay_caps.set_value("payload", 123)
                rtpvppay_capsfilter = Gst.ElementFactory.make("capsfilter")
                rtpvppay_capsfilter.set_property("caps", rtpvppay_caps)

            # VPX Parameters
            # Borrowed from: https://github.com/nurdism/neko/blob/df98368137732b8aaf840e27cdf2bd41067b2161/server/internal/gst/gst.go#L94
            vpenc.set_property("threads", 4)
            vpenc.set_property("cpu-used", 8)
            vpenc.set_property("deadline", 1)
            vpenc.set_property("error-resilient", "partitions")
            vpenc.set_property("keyframe-max-dist", 10)
            vpenc.set_property("auto-alt-ref", True)
            vpenc.set_property("target-bitrate", self.video_bitrate*1000)

        else:
            raise GSTWebRTCAppError("Unsupported encoder for pipeline: %s" % self.encoder)

        # Add all elements to the pipeline.
        self.pipeline.add(ximagesrc)
        self.pipeline.add(ximagesrc_capsfilter)

        if self.encoder == "nvh264enc":
            self.pipeline.add(cudaupload)
            self.pipeline.add(cudaconvert)
            self.pipeline.add(cudaconvert_capsfilter)
            self.pipeline.add(nvh264enc)
            self.pipeline.add(nvh264enc_capsfilter)
            self.pipeline.add(rtph264pay)
            self.pipeline.add(rtph264pay_capsfilter)

        if self.encoder == "x264enc":
            self.pipeline.add(videoconvert)
            self.pipeline.add(videoconvert_capsfilter)
            self.pipeline.add(x264enc)
            self.pipeline.add(x264enc_capsfilter)
            self.pipeline.add(rtph264pay)
            self.pipeline.add(rtph264pay_capsfilter)

        elif self.encoder.startswith("vp"):
            self.pipeline.add(videoconvert)
            self.pipeline.add(videoconvert_capsfilter)
            self.pipeline.add(vpenc)
            self.pipeline.add(vpenc_capsfilter)
            self.pipeline.add(rtpvppay)
            self.pipeline.add(rtpvppay_capsfilter)

        # Link the pipeline elements and raise exception of linking fails
        # due to incompatible element pad capabilities.
        if not Gst.Element.link(ximagesrc, ximagesrc_capsfilter):
            raise GSTWebRTCAppError("Failed to link ximagesrc -> ximagesrc_capsfilter")

        if self.encoder == "nvh264enc":
            if not Gst.Element.link(ximagesrc_capsfilter, cudaupload):
                raise GSTWebRTCAppError(
                    "Failed to link ximagesrc_capsfilter -> cudaupload")

            if not Gst.Element.link(cudaupload, cudaconvert):
                raise GSTWebRTCAppError(
                    "Failed to link cudaupload -> cudaconvert")

            if not Gst.Element.link(cudaconvert, cudaconvert_capsfilter):
                raise GSTWebRTCAppError(
                    "Failed to link cudaconvert -> cudaconvert_capsfilter")

            if not Gst.Element.link(cudaconvert_capsfilter, nvh264enc):
                raise GSTWebRTCAppError(
                    "Failed to link cudaconvert_capsfilter -> nvh264enc")

            if not Gst.Element.link(nvh264enc, nvh264enc_capsfilter):
                raise GSTWebRTCAppError(
                    "Failed to link nvh264enc -> nvh264enc_capsfilter")

            if not Gst.Element.link(nvh264enc_capsfilter, rtph264pay):
                raise GSTWebRTCAppError(
                    "Failed to link nvh264enc_capsfilter -> rtph264pay")

            if not Gst.Element.link(rtph264pay, rtph264pay_capsfilter):
                raise GSTWebRTCAppError(
                    "Failed to link rtph264pay -> rtph264pay_capsfilter")

            # Link the last element to the webrtcbin
            if not Gst.Element.link(rtph264pay_capsfilter, self.webrtcbin):
                raise GSTWebRTCAppError(
                    "Failed to link rtph264pay_capsfilter -> webrtcbin")

        elif self.encoder == "x264enc":
            if not Gst.Element.link(ximagesrc_capsfilter, videoconvert):
                raise GSTWebRTCAppError(
                    "Failed to link ximagesrc_capsfilter -> videoconvert")

            if not Gst.Element.link(videoconvert, videoconvert_capsfilter):
                raise GSTWebRTCAppError(
                    "Failed to link videoconvert -> videoconvert_capsfilter")

            if not Gst.Element.link(videoconvert_capsfilter, x264enc):
                raise GSTWebRTCAppError(
                    "Failed to link videoconvert_capsfilter -> x264enc")

            if not Gst.Element.link(x264enc, x264enc_capsfilter):
                raise GSTWebRTCAppError(
                    "Failed to link x264enc -> x264enc_capsfilter")

            if not Gst.Element.link(x264enc_capsfilter, rtph264pay):
                raise GSTWebRTCAppError(
                    "Failed to link x264enc_capsfilter -> rtph264pay")

            if not Gst.Element.link(rtph264pay, rtph264pay_capsfilter):
                raise GSTWebRTCAppError(
                    "Failed to link rtph264pay -> rtph264pay_capsfilter")

            # Link the last element to the webrtcbin
            if not Gst.Element.link(rtph264pay_capsfilter, self.webrtcbin):
                raise GSTWebRTCAppError(
                    "Failed to link rtph264pay_capsfilter -> webrtcbin")

        elif self.encoder.startswith("vp"):
            if not Gst.Element.link(ximagesrc_capsfilter, videoconvert):
                raise GSTWebRTCAppError(
                    "Failed to link ximagesrc_capsfilter -> videoconvert")

            if not Gst.Element.link(videoconvert, videoconvert_capsfilter):
                raise GSTWebRTCAppError(
                    "Failed to link videoconvert -> videoconvert_capsfilter")

            if not Gst.Element.link(videoconvert_capsfilter, vpenc):
                raise GSTWebRTCAppError(
                    "Failed to link videoconvert_capsfilter -> vpenc")

            if not Gst.Element.link(vpenc, vpenc_capsfilter):
                raise GSTWebRTCAppError(
                    "Failed to link vpenc -> vpenc_capsfilter")

            if not Gst.Element.link(vpenc_capsfilter, rtpvppay):
                raise GSTWebRTCAppError(
                    "Failed to link vpenc_capsfilter -> rtpvppay")

            if not Gst.Element.link(rtpvppay, rtpvppay_capsfilter):
                raise GSTWebRTCAppError(
                    "Failed to link rtpvppay -> rtpvppay_capsfilter")

            # Link the last element to the webrtcbin
            if not Gst.Element.link(rtpvppay_capsfilter, self.webrtcbin):
                raise GSTWebRTCAppError(
                    "Failed to link rtpvppay_capsfilter -> webrtcbin")
    def check_plugins(self):
        """Check for required gstreamer plugins.

        Raises:
            GSTWebRTCAppError -- thrown if any plugins are missing.
        """

        required = ["opus", "nice", "webrtc", "dtls", "srtp", "rtp", "sctp", "rtpmanager", "ximagesrc"]

        # ADD_ENCODER: add new encoder to this list
        supported = ["nvcudah264enc", "nvh264enc", "nvcudah265enc", "nvh265enc", "vah264enc", "vah264lpenc", "vah265enc", "vah265lpenc", "vavp9enc", "vavp9lpenc", "vaav1enc", "vaav1lpenc", "x264enc", "openh264enc", "x265enc", "vp8enc", "vp9enc", "rav1enc"]
        if self.encoder not in supported:
            raise GSTWebRTCAppError('Unsupported encoder, must be one of: ' + ','.join(supported))

        # ADD_ENCODER: add new encoder to this list
        if self.encoder.startswith("nv"):
            required.append("nvcodec")

        elif self.encoder.startswith("va"):
            required.append("va")

        elif self.encoder in ["x264enc"]:
            required.append("x264")

        elif self.encoder in ["openh264enc"]:
            required.append("openh264")

        elif self.encoder in ["x265enc"]:
            required.append("x265")

        elif self.encoder in ["vp8enc", "vp9enc"]:
            required.append("vpx")

        elif self.encoder in ["rav1enc"]:
            required.append("rav1e")

        missing = list(
            filter(lambda p: Gst.Registry.get().find_plugin(p) is None, required))
        if missing:
            raise GSTWebRTCAppError('Missing gstreamer plugins:', missing)

    def set_sdp(self, sdp_type, sdp):
        """Sets remote SDP received by peer.

        Arguments:
            sdp_type {string} -- type of sdp, offer or answer
            sdp {object} -- SDP object

        Raises:
            GSTWebRTCAppError -- thrown if SDP is recevied before session has been started.
            GSTWebRTCAppError -- thrown if SDP type is not 'answer', this script initiates the call, not the peer.
        """

        if not self.webrtcbin:
            raise GSTWebRTCAppError('Received SDP before session started')

        if sdp_type != 'answer':
            raise GSTWebRTCAppError('ERROR: sdp type was not "answer"')

        _, sdpmsg = GstSdp.SDPMessage.new_from_text(sdp)
        answer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
        promise = Gst.Promise.new()
        self.webrtcbin.emit('set-remote-description', answer, promise)
        promise.interrupt()

    def set_ice(self, mlineindex, candidate):
        """Adds ice candidate received from signalling server

        Arguments:
            mlineindex {integer} -- the mlineindex
            candidate {string} -- the candidate

        Raises:
            GSTWebRTCAppError -- thrown if called before session is started.
        """

        logger.info("setting ICE candidate: %d, %s" % (mlineindex, candidate))

        if not self.webrtcbin:
            raise GSTWebRTCAppError('Received ICE before session started')

        self.webrtcbin.emit('add-ice-candidate', mlineindex, candidate)

    def set_framerate(self, framerate):
        """Set pipeline framerate in fps

        Arguments:
            framerate {integer} -- framerate in frames per second, for example, 15, 30, 60.
        """
        self.framerate = framerate
        self.ximagesrc_caps = Gst.caps_from_string("video/x-raw")
        self.ximagesrc_caps.set_value("framerate", Gst.Fraction(self.framerate, 1))
        self.ximagesrc_capsfilter.set_property("caps", self.ximagesrc_caps)
        logger.info("framerate set to: %d" % framerate)

        # ADD_ENCODER: GOP/IDR Keyframe distance to keep the stream from freezing (in keyframe_dist seconds)
        if self.keyframe_distance != -1.0:
            if self.encoder.startswith("nv"):
                element = Gst.Bin.get_by_name(self.pipeline, "nvenc")
                element.set_property("gop-size", -1 if self.keyframe_distance == -1.0 else int(self.framerate * self.keyframe_distance))
            elif self.encoder.startswith("va"):
                element = Gst.Bin.get_by_name(self.pipeline, "vaenc")
                element.set_property("key-int-max", 0 if self.keyframe_distance == -1.0 else int(self.framerate * self.keyframe_distance))
            elif self.encoder in ["x264enc"]:
                element = Gst.Bin.get_by_name(self.pipeline, "x264enc")
                element.set_property("key-int-max", 2147483647 if self.keyframe_distance == -1.0 else int(self.framerate * self.keyframe_distance))
            elif self.encoder in ["openh264enc"]:
                element = Gst.Bin.get_by_name(self.pipeline, "openh264enc")
                element.set_property("gop-size", 2147483647 if self.keyframe_distance == -1.0 else int(self.framerate * self.keyframe_distance))
            elif self.encoder in ["x265enc"]:
                element = Gst.Bin.get_by_name(self.pipeline, "x265enc")
                element.set_property("key-int-max", 2147483647 if self.keyframe_distance == -1.0 else int(self.framerate * self.keyframe_distance))
            elif self.encoder.startswith("vp"):
                element = Gst.Bin.get_by_name(self.pipeline, "vpenc")
                element.set_property("keyframe-max-dist", 2147483647 if self.keyframe_distance == -1.0 else int(self.framerate * self.keyframe_distance))
            elif self.encoder in ["rav1enc"]:
                element = Gst.Bin.get_by_name(self.pipeline, "rav1enc")
                element.set_property("max-key-frame-interval", 715827882 if self.keyframe_distance == -1.0 else int(self.framerate * self.keyframe_distance))
            else:
                logger.warning("setting keyframe interval (GOP size) not supported with encoder: %s" % self.encoder)

    def set_video_bitrate(self, bitrate, cc=False):
        """Set video encoder target bitrate in bps

        Arguments:
            bitrate {integer} -- bitrate in bits per second, for example, 2000 for 2kbits/s or 10000 for 1mbit/sec.
            cc {boolean} -- whether the congestion control element triggered the bitrate change.
        """

        if self.pipeline:
            # Prevent bitrate from overshooting because of FEC
            fec_bitrate = int(bitrate / (1.0 + (self.video_packetloss_percent / 100.0)))
            # Change bitrate range of congestion control element
            if (not cc) and self.congestion_control and self.rtpgccbwe is not None:
                # Prevent encoder freeze because of low bitrate with min-bitrate
                self.rtpgccbwe.set_property("min-bitrate", max(100000 + self.audio_bitrate, int(bitrate * 1000 * 0.1 + self.audio_bitrate)))
                self.rtpgccbwe.set_property("max-bitrate", int(bitrate * 1000 + self.audio_bitrate))
                # Method is called again through the notifier with cc=True
                self.rtpgccbwe.set_property("estimated-bitrate", int(bitrate * 1000 + self.audio_bitrate))
                return
            # ADD_ENCODER: add new encoder to this list
            if self.encoder.startswith("nv"):
                element = Gst.Bin.get_by_name(self.pipeline, "nvenc")
                element.set_property("bitrate", fec_bitrate)
            elif self.encoder.startswith("va"):
                element = Gst.Bin.get_by_name(self.pipeline, "vaenc")
                element.set_property("bitrate", fec_bitrate)
            elif self.encoder in ["x264enc"]:
                element = Gst.Bin.get_by_name(self.pipeline, "x264enc")
                element.set_property("bitrate", fec_bitrate)
            elif self.encoder in ["openh264enc"]:
                element = Gst.Bin.get_by_name(self.pipeline, "openh264enc")
                element.set_property("bitrate", fec_bitrate * 1000)
            elif self.encoder in ["x265enc"]:
                element = Gst.Bin.get_by_name(self.pipeline, "x265enc")
                element.set_property("bitrate", fec_bitrate)
            elif self.encoder in ["vp8enc", "vp9enc"]:
                element = Gst.Bin.get_by_name(self.pipeline, "vpenc")
                element.set_property("target-bitrate", fec_bitrate * 1000)
            elif self.encoder in ["rav1enc"]:
                element = Gst.Bin.get_by_name(self.pipeline, "rav1enc")
                element.set_property("bitrate", fec_bitrate)
            else:
                logger.warning("set_video_bitrate not supported with encoder: %s" % self.encoder)

            if not cc:
                logger.info("video bitrate set to: %d" % bitrate)
            else:
                logger.debug("video bitrate set with congestion control to: %d" % bitrate)

            self.video_bitrate = bitrate
            self.fec_video_bitrate = fec_bitrate

            self.__send_data_channel_message(
                "pipeline", {"status": "Video bitrate set to: %d" % bitrate})

    def set_audio_bitrate(self, bitrate):
        """Set Opus encoder target bitrate in bps

        Arguments:
            bitrate {integer} -- bitrate in bits per second, for example, 96000 for 96 kbits/s.
        """

        if self.pipeline:
            # Prevent bitrate from overshooting because of FEC
            fec_bitrate = int(bitrate / (1.0 + (self.audio_packetloss_percent / 100.0)))
            # Change bitrate range of congestion control element
            if self.congestion_control and self.rtpgccbwe is not None:
                # Prevent encoder freeze because of low bitrate with min-bitrate
                self.rtpgccbwe.set_property("min-bitrate", max(100000 + bitrate, int(self.video_bitrate * 1000 * 0.1 + bitrate)))
                self.rtpgccbwe.set_property("max-bitrate", int(self.video_bitrate * 1000 + bitrate))
                self.rtpgccbwe.set_property("estimated-bitrate", int(self.video_bitrate * 1000 + bitrate))
            element = Gst.Bin.get_by_name(self.pipeline, "opusenc")
            element.set_property("bitrate", fec_bitrate)

            if not cc:
                logger.info("audio bitrate set to: %d" % bitrate)
            else:
                logger.debug("audio bitrate set with congestion control to: %d" % bitrate)
            self.audio_bitrate = bitrate
            self.fec_audio_bitrate = fec_bitrate
            self.__send_data_channel_message(
                "pipeline", {"status": "Audio bitrate set to: %d" % bitrate})

    def set_pointer_visible(self, visible):
        """Set pointer visibiltiy on the ximagesrc element

        Arguments:
            visible {bool} -- True to enable pointer visibility
        """

        element = Gst.Bin.get_by_name(self.pipeline, "x11")
        element.set_property("show-pointer", visible)
        self.__send_data_channel_message(
            "pipeline", {"status": "Set pointer visibility to: %d" % visible})

    def send_clipboard_data(self, data):
        # TODO: WebRTC DataChannel accepts a maximum length of 65489 (= 65535 - 46 for '{"type": "clipboard", "data": {"content": ""}}'), remove this restriction after implementing DataChannel chunking
        CLIPBOARD_RESTRICTION = 65400
        clipboard_message = base64.b64encode(data.encode()).decode("utf-8")
        clipboard_length = len(clipboard_message)
        if clipboard_length <= CLIPBOARD_RESTRICTION:
            self.__send_data_channel_message(
                "clipboard", {"content": clipboard_message})
        else:
            logger.warning("clipboard may not be sent to the client because the base64 message length {} is above the maximum length of {}".format(clipboard_length, CLIPBOARD_RESTRICTION))

    def send_cursor_data(self, data):
        self.last_cursor_sent = data
        self.__send_data_channel_message(
            "cursor", data)

    def send_gpu_stats(self, load, memory_total, memory_used):
        """Sends GPU stats to the data channel

        Arguments:
            load {float} -- utilization of GPU between 0 and 1
            memory_total {float} -- total memory on GPU in MB
            memory_used {float} -- memor used on GPU in MB
        """

        self.__send_data_channel_message("gpu_stats", {
            "load": load,
            "memory_total": memory_total,
            "memory_used": memory_used,
        })

    def send_reload_window(self):
        """Sends reload window command to the data channel
        """
        logger.info("sending window reload")
        self.__send_data_channel_message(
            "system", {"action": "reload"})

    def send_framerate(self, framerate):
        """Sends the current framerate to the data channel
        """
        logger.info("sending framerate")
        self.__send_data_channel_message(
            "system", {"action": "framerate,"+str(framerate)})

    def send_video_bitrate(self, bitrate):
        """Sends the current video bitrate to the data channel
        """
        logger.info("sending video bitrate")
        self.__send_data_channel_message(
            "system", {"action": "video_bitrate,%d" % bitrate})

    def send_audio_bitrate(self, bitrate):
        """Sends the current audio bitrate to the data channel
        """
        logger.info("sending audio bitrate")
        self.__send_data_channel_message(
            "system", {"action": "audio_bitrate,%d" % bitrate})

    def send_encoder(self, encoder):
        """Sends the encoder name to the data channel
        """
        logger.info("sending encoder: " + encoder)
        self.__send_data_channel_message(
            "system", {"action": "encoder,%s" % encoder})

    def send_resize_enabled(self, resize_enabled):
        """Sends the current resize enabled state
        """
        logger.info("sending resize enabled state")
        self.__send_data_channel_message(
            "system", {"action": "resize,"+str(resize_enabled)})

    def send_remote_resolution(self, res):
        """sends the current remote resolution to the client
        """
        logger.info("sending remote resolution of: " + res)
        self.__send_data_channel_message(
            "system", {"action": "resolution," + res})

    def send_ping(self, t):
        """Sends a ping request over the data channel to measure latency
        """
        self.__send_data_channel_message(
            "ping", {"start_time": float("%.3f" % t)})

    def send_latency_time(self, latency):
        """Sends measured latency response time in ms
        """
        self.__send_data_channel_message(
            "latency_measurement", {"latency_ms": latency})

    def send_system_stats(self, cpu_percent, mem_total, mem_used):
        """Sends system stats
        """
        self.__send_data_channel_message(
            "system_stats", {
                "cpu_percent": cpu_percent,
                "mem_total": mem_total,
                "mem_used": mem_used,
            })

    def is_data_channel_ready(self):
        """Checks to see if the data channel is open.

        Returns:
            [bool] -- true if data channel is open
        """
        return self.data_channel and self.data_channel.get_property("ready-state") == GstWebRTC.WebRTCDataChannelState.OPEN

    def __send_data_channel_message(self, msg_type, data):
        """Sends message to the peer through the data channel

        Message is dropped if the channel is not open.

        Arguments:
            msg_type {string} -- the type of message being sent
            data {dict} -- data to send, this is JSON serialized.
        """
        if not self.is_data_channel_ready():
            logger.debug(
                "skipping message because data channel is not ready: %s" % msg_type)
            return

        msg = {"type": msg_type, "data": data}
        self.data_channel.emit("send-string", json.dumps(msg))

    def __on_offer_created(self, promise, _, __):
        """Handles on-offer-created promise resolution

        The offer contains the local description.
        Generate a set-local-description action with the offer.
        Sends the offer to the on_sdp handler.

        Arguments:
            promise {GstPromise} -- the promise
            _ {object} -- unused
            __ {object} -- unused
        """

        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value('offer')
        promise = Gst.Promise.new()
        self.webrtcbin.emit('set-local-description', offer, promise)
        promise.interrupt()
        loop = asyncio.new_event_loop()
        sdp_text = offer.sdp.as_text()
        # rtx-time needs to be set to 125 milliseconds for optimal performance
        if 'rtx-time' not in sdp_text:
            logger.warning("injecting rtx-time to SDP")
            sdp_text = re.sub(r'(apt=\d+)', r'\1;rtx-time=125', sdp_text)
        elif 'rtx-time=125' not in sdp_text:
            logger.warning("injecting modified rtx-time to SDP")
            sdp_text = re.sub(r'rtx-time=\d+', r'rtx-time=125', sdp_text)
        # Firefox needs profile-level-id=42e01f in the offer, but webrtcbin does not add this.
        # TODO: Remove when fixed in webrtcbin.
        #   https://gitlab.freedesktop.org/gstreamer/gstreamer/-/issues/1106
        if "h264" in self.encoder or "x264" in self.encoder:
            if 'profile-level-id' not in sdp_text:
                logger.warning("injecting profile-level-id to SDP")
                sdp_text = sdp_text.replace('packetization-mode=1', 'profile-level-id=42e01f;packetization-mode=1')
            if 'level-asymmetry-allowed' not in sdp_text:
                logger.warning("injecting level-asymmetry-allowed to SDP")
                sdp_text = sdp_text.replace('packetization-mode=1', 'level-asymmetry-allowed=1;packetization-mode=1')
        loop.run_until_complete(self.on_sdp('offer', sdp_text))

    def __request_aux_sender(self, webrtcbin, dtls_transport):
        """Handles request-aux-header signal, initializing the rtpgccbwe element for WebRTC

        Arguments:
            webrtcbin {GstWebRTCBin gobject} -- webrtcbin gobject
            dtls_transport {GstWebRTCDTLSTransport gobject} -- DTLS Transport for which the aux sender will be used
        """
        self.rtpgccbwe = Gst.ElementFactory.make("rtpgccbwe")
        if self.rtpgccbwe is None:
            logger.warning("rtpgccbwe element is not available, not performing any congestion control.")
            return None
        logger.info("handling on-request-aux-header, activating rtpgccbwe congestion control.")
        # Prevent encoder freeze because of low bitrate with min-bitrate
        self.rtpgccbwe.set_property("min-bitrate", max(100000 + self.audio_bitrate, int(self.video_bitrate * 1000 * 0.1 + self.audio_bitrate)))
        self.rtpgccbwe.set_property("max-bitrate", int(self.video_bitrate * 1000 + self.audio_bitrate))
        self.rtpgccbwe.set_property("estimated-bitrate", int(self.video_bitrate * 1000 + self.audio_bitrate))
        self.rtpgccbwe.connect("notify::estimated-bitrate", lambda bwe, pspec: self.set_video_bitrate(int((bwe.get_property(pspec.name) - self.audio_bitrate) / 1000), cc=True))
        return self.rtpgccbwe

    def __pick_twcc_extension_id(self, payloader):
        """Finds extension ID for Transport-Wide Congestion Control (TWCC), required for rtpgccbwe

        Arguments:
            payloader {GstRTPBasePayload gobject} -- payloader gobject
        """
        payloader_properties = payloader.list_properties()
        enabled_extensions = payloader.get_property("extensions") if "extensions" in [payloader_property.name for payloader_property in payloader_properties] else None
        if not enabled_extensions:
            logger.debug("'extensions' property in {} does not exist in payloader, application code must ensure to select non-conflicting IDs for any additionally configured extensions".format(payloader.get_name()))
            return 1
        twcc = next((ext for ext in enabled_extensions if ext.get_uri() == self.RTP_TWCC_URI), None)
        # When TWCC is already mapped
        if twcc:
            return None
        used_numbers = set(ext.get_id() for ext in enabled_extensions)
        # Find first extension ID that does not collide
        num = 1
        while True:
            if num not in used_numbers:
                return num
            num += 1

    def __on_negotiation_needed(self, webrtcbin):
        """Handles on-negotiation-needed signal, generates create-offer action

        Arguments:
            webrtcbin {GstWebRTCBin gobject} -- webrtcbin gobject
        """
        logger.info("handling on-negotiation-needed, creating offer.")
        promise = Gst.Promise.new_with_change_func(
            self.__on_offer_created, webrtcbin, None)
        webrtcbin.emit('create-offer', None, promise)

    def __send_ice(self, webrtcbin, mlineindex, candidate):
        """Handles on-ice-candidate signal, generates on_ice event

        Arguments:
            webrtcbin {GstWebRTCBin gobject} -- webrtcbin gobject
            mlineindex {integer} -- ice candidate mlineindex
            candidate {string} -- ice candidate string
        """
        logger.debug("received ICE candidate: %d %s", mlineindex, candidate)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.on_ice(mlineindex, candidate))

    def bus_call(self, message):
        t = message.type
        if t == Gst.MessageType.EOS:
            logger.error("End-of-stream\n")
            return False
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error("Error: %s: %s\n" % (err, debug))
            return False
        elif t == Gst.MessageType.STATE_CHANGED:
            if isinstance(message.src, Gst.Pipeline):
                old_state, new_state, pending_state = message.parse_state_changed()
                logger.info(("Pipeline state changed from %s to %s." %
                    (old_state.value_nick, new_state.value_nick)))
                if (old_state.value_nick == "paused" and new_state.value_nick == "ready"):
                    logger.info("stopping bus message loop")
                    return False
        elif t == Gst.MessageType.LATENCY:
            if self.pipeline:
                return_output = self.pipeline.recalculate_latency()
                if not return_output:
                    logger.warning("failed to recalculate pipeline latency")
        return True

    def start_pipeline(self, audio_only=False):
        """Starts the GStreamer pipeline
        """

        logger.info("starting pipeline")

        self.pipeline = Gst.Pipeline.new()

        # Construct the webrtcbin pipeline
        self.build_webrtcbin_pipeline(audio_only)

        if audio_only:
            self.build_audio_pipeline()
        else:
            self.build_video_pipeline()

        # Advance the state of the pipeline to PLAYING.
        res = self.pipeline.set_state(Gst.State.PLAYING)
        if res != Gst.StateChangeReturn.SUCCESS:
            raise GSTWebRTCAppError(
                "Failed to transition pipeline to PLAYING: %s" % res)

        if not audio_only:
            # Create the data channel, this has to be done after the pipeline is PLAYING.
            options = Gst.Structure("application/data-channel")
            options.set_value("ordered", True)
            options.set_value("max-retransmits", 0)
            self.data_channel = self.webrtcbin.emit(
                'create-data-channel', "input", options)
            self.data_channel.connect('on-open', lambda _: self.on_data_open())
            self.data_channel.connect('on-close', lambda _: self.on_data_close())
            self.data_channel.connect('on-error', lambda _: self.on_data_error())
            self.data_channel.connect(
                'on-message-string', lambda _, msg: self.on_data_message(msg))

        logger.info("{} pipeline started".format("audio" if audio_only else "video"))

    async def handle_bus_calls(self):
        # Start bus call loop
        running = True
        bus = None
        while running:
            if self.pipeline is not None:
                bus = self.pipeline.get_bus()
            if bus is not None:
                while bus.have_pending():
                    msg = bus.pop()
                    if not self.bus_call(msg):
                        running = False
            await asyncio.sleep(0.1)

    def stop_pipeline(self):
        logger.info("stopping pipeline")
        if self.data_channel:
            self.data_channel.emit('close')
            self.data_channel = None
            logger.info("data channel closed")
        if self.pipeline:
            logger.info("setting pipeline state to NULL")
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            logger.info("pipeline set to state NULL")
        if self.webrtcbin:
            self.webrtcbin.set_state(Gst.State.NULL)
            self.webrtcbin = None
            logger.info("webrtcbin set to state NULL")
        logger.info("pipeline stopped")
