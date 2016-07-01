import sys
import signal

from threading import Thread

from .actions import ActionRegistry
from .backends import BluetoothBackend, HidrawBackend
from .config import load_options
from .daemon import Daemon
from .eventloop import EventLoop
from .exceptions import BackendError
from .audio import pulseaudio_sbc_stream, StreamReader


class DS4Controller(object):
    def __init__(self, index, options, stream_reader, dynamic=False):
        self.index = index
        self.dynamic = dynamic
        self.logger = Daemon.logger.new_module("controller {0}".format(index))

        self.error = None
        self.device = None
        self.loop = EventLoop()

        self.actions = [cls(self) for cls in ActionRegistry.actions]
        self.bindings = options.parent.bindings
        self.current_profile = "default"
        self.default_profile = options
        self.options = self.default_profile
        self.profiles = options.profiles
        self.profile_options = dict(options.parent.profiles)
        self.profile_options["default"] = self.default_profile

        if self.profiles:
            self.profiles.append("default")

        self.stream_reader = stream_reader

        self.load_options(self.options)

    def fire_event(self, event, *args):
        self.loop.fire_event(event, *args)

    def load_profile(self, profile):
        if profile == self.current_profile:
            return

        profile_options = self.profile_options.get(profile)
        if profile_options:
            self.logger.info("Switching to profile: {0}", profile)
            self.load_options(profile_options)
            self.current_profile = profile
            self.fire_event("load-profile", profile)
        else:
            self.logger.warning("Ignoring invalid profile: {0}", profile)

    def next_profile(self):
        if not self.profiles:
            return

        next_index = self.profiles.index(self.current_profile) + 1
        if next_index >= len(self.profiles):
            next_index = 0

        self.load_profile(self.profiles[next_index])

    def prev_profile(self):
        if not self.profiles:
            return

        next_index = self.profiles.index(self.current_profile) - 1
        if next_index < 0:
            next_index = len(self.profiles) - 1

        self.load_profile(self.profiles[next_index])

    def setup_device(self, device):
        self.logger.info("Connected to {0}", device.name)

        self.device = device
        self.device.set_led(*self.options.led)
        self.fire_event("device-setup", device)
        self.loop.add_watcher(device.report_fd, self.read_report)
        self.load_options(self.options)

    def cleanup_device(self):
        self.logger.info("Disconnected")
        self.fire_event("device-cleanup")
        self.loop.remove_watcher(self.device.report_fd)
        self.device.close()
        self.device = None

        if self.dynamic:
            self.loop.stop()

    def load_options(self, options):
        self.fire_event("load-options", options)
        self.options = options

    def read_report(self):
        report = self.device.read_report()

        if not report:
            if report is False:
                return

            self.cleanup_device()
            return

        self.fire_event("device-report", report)

    def run(self):
        self.loop.run()

    def exit(self, *args, error = True):
        if self.device:
            self.cleanup_device()

        if error == True:
            self.logger.error(*args)
            self.error = True
        else:
            self.logger.info(*args)


def create_controller_thread(index, controller_options, stream_reader,
                             dynamic=False):
    controller = DS4Controller(index, controller_options, stream_reader,
                               dynamic=dynamic)

    thread = Thread(target=controller.run)
    thread.controller = controller
    thread.start()

    return thread


class SigintHandler(object):
    def __init__(self, threads, stream_reader):
        self.threads = threads
        self.stream_reader = stream_reader

    def cleanup_controller_threads(self):
        for thread in self.threads:
            thread.controller.exit("Cleaning up...", error=False)
            thread.controller.loop.stop()
            thread.join()

    def cleanup_stream_reader(self):
        print("stopping stream_reader")
        self.stream_reader.stop()
        print("joining stream_reader thread")
        print("joined")

    def __call__(self, signum, frame):
        signal.signal(signum, signal.SIG_DFL)

        print("Running SIGINT")

        self.cleanup_stream_reader()
        self.cleanup_controller_threads()

        sys.exit(0)


def main():
    threads = []
    stream_reader = StreamReader(
        "ds4drv", "Test\\ ds4drv\\ sink"
    )

    sigint_handler = SigintHandler(threads, stream_reader)
    signal.signal(signal.SIGINT, sigint_handler)

    #while stream_reader.sbc_frames_waiting() == False:
    #    import time
    #    time.sleep(1)
    stream_reader.start()


    try:
        options = load_options()
    except ValueError as err:
        Daemon.exit("Failed to parse options: {0}", err)

    if options.hidraw:
        backend = HidrawBackend(Daemon.logger)
    else:
        backend = BluetoothBackend(Daemon.logger)

    try:
        backend.setup()
    except BackendError as err:
        Daemon.exit(err)

    if options.daemon:
        Daemon.fork(options.daemon_log, options.daemon_pid)

    for index, controller_options in enumerate(options.controllers):
        thread = create_controller_thread(
            index + 1, controller_options, stream_reader
        )
        threads.append(thread)

    for device in backend.devices:
        print("-----")
        connected_devices = []
        for thread in threads:
            # Controller has received a fatal error, exit
            if thread.controller.error:
                sys.exit(1)

            if thread.controller.device:
                connected_devices.append(thread.controller.device.device_addr)

            # Clean up dynamic threads
            if not thread.is_alive():
                threads.remove(thread)

        if device.device_addr in connected_devices:
            backend.logger.warning("Ignoring already connected device: {0}",
                                   device.device_addr)
            continue

        for thread in filter(lambda t: not t.controller.device, threads):
            break
        else:
            thread = create_controller_thread(len(threads) + 1,
                                              options.default_controller,
                                              stream_reader,
                                              dynamic=True)
            threads.append(thread)

        thread.controller.setup_device(device)

if __name__ == "__main__":
    main()
