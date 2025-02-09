import datetime
import os
import re
import socket
import subprocess
from telnetlib import Telnet
import time

from emulator_battery import EmulatorBattery


class Emulator(object):

    deviceRe = re.compile(r"^emulator-(\d+)(\s*)(.*)$")

    def __init__(self, homedir=None):
        self.port = None
        self._emulator_launched = False
        self.proc = None
        self.marionette_port = None
        self.telnet = None
        self.battery = EmulatorBattery(self)
        self.homedir = homedir

    def _check_for_b2g(self):
        if self.homedir is None:
            self.homedir = os.getenv('B2G_HOME')
        if self.homedir is None:
            raise Exception('Must define B2G_HOME or pass the homedir parameter')

        self.adb = os.path.join(self.homedir,
                                'glue/gonk/out/host/linux-x86/bin/adb')
        if not os.access(self.adb, os.F_OK):
            self.adb = os.path.join(self.homedir, 'bin/adb')

        self.binary = os.path.join(self.homedir,
                                   'glue/gonk/out/host/linux-x86/bin/emulator')
        if not os.access(self.binary, os.F_OK):
            self.binary = os.path.join(self.homedir, 'bin/emulator')
        self._check_file(self.binary)

        self.kernelImg = os.path.join(self.homedir,
                                      'boot/kernel-android-qemu/arch/arm/boot/zImage')
        if not os.access(self.kernelImg, os.F_OK):
            self.kernelImg = os.path.join(self.homedir, 'zImage')
        self._check_file(self.kernelImg)

        self.sysDir = os.path.join(self.homedir, 
                                   'glue/gonk/out/target/product/generic/')
        if not os.access(self.sysDir, os.F_OK):
            self.sysDir = os.path.join(self.homedir, 'generic/')
        self._check_file(self.sysDir)

        self.dataImg = os.path.join(self.sysDir, 'userdata.img')
        self._check_file(self.dataImg)

    def __del__(self):
        if self.telnet:
            self.telnet.write('exit\n')
            self.telnet.read_all()

    def _check_file(self, filePath):
        if not os.access(filePath, os.F_OK):
            raise Exception(('File not found: %s; did you pass the B2G home '
                             'directory as the homedir parameter, or set '
                             'B2G_HOME correctly?') % filePath)

    @property
    def args(self):
        return [
                    self.binary,
                    '-kernel', self.kernelImg,
                    '-sysdir', self.sysDir,
                    '-data', self.dataImg,
                    '-memory', '512',
                    '-verbose',
                    '-qemu', '-cpu', 'cortex-a8'
               ]

    @property
    def is_running(self):
        if self._emulator_launched:
            return self.proc is not None and self.proc.poll() is None
        else:
            return self.port is not None

    def _check_for_adb(self):
        adb = subprocess.Popen(['which', 'adb'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        retcode = adb.wait()
        if retcode:
            raise Exception('which terminated with exit code %d: %s' 
                            % (retcode, adb.stdout.read()))
        out = adb.stdout.read().strip()
        if len(out) and out.find('/') > -1:
            self.adb = out

    def _run_adb(self, args):
        args.insert(0, self.adb)
        adb = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        retcode = adb.wait()
        if retcode:
            raise Exception('adb terminated with exit code %d: %s' 
                            % (retcode, adb.stdout.read()))
        return adb.stdout.read()

    def _get_telnet_response(self, command=None):
        output = []
        assert(self.telnet)
        if command is not None:
            self.telnet.write('%s\n' % command)
        while True:
            line = self.telnet.read_until('\n')
            output.append(line.rstrip())
            if line.startswith('OK'):
                return output
            elif line.startswith('KO:'):
                raise Exception ('bad telnet response: %s' % line)

    def _run_telnet(self, command):
        if not self.telnet:
            self.telnet = Telnet('localhost', self.port)
            self._get_telnet_response()
        return self._get_telnet_response(command)

    def close(self):
        if self.is_running and self._emulator_launched:
            self.proc.terminate()
            self.proc.wait()
        if self.proc:
            retcode = self.proc.poll()
            self.proc = None
            return retcode

    def _get_adb_devices(self):
        offline = set()
        online = set()
        output = self._run_adb(['devices'])
        for line in output.split('\n'):
            m = self.deviceRe.match(line)
            if m:
                if m.group(3) == 'offline':
                    offline.add(m.group(1))
                else:
                    online.add(m.group(1))
        return (online, offline)

    def connect(self):
        self._check_for_adb()
        self._run_adb(['start-server'])

        online, offline = self._get_adb_devices()
        now = datetime.datetime.now()
        while online == set([]):
            time.sleep(1)
            if datetime.datetime.now() - now > datetime.timedelta(seconds=60):
                raise Exception('timed out waiting for emulator to be available')
            online, offline = self._get_adb_devices()
        self.port = int(list(online)[0])

    def start(self):
        self._check_for_b2g()
        self._run_adb(['start-server'])

        original_online, original_offline = self._get_adb_devices()

        self.proc = subprocess.Popen(self.args,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)

        online, offline = self._get_adb_devices()
        now = datetime.datetime.now()
        while online - original_online == set([]):
            time.sleep(1)
            if datetime.datetime.now() - now > datetime.timedelta(seconds=60):
                raise Exception('timed out waiting for emulator to start')
            online, offline = self._get_adb_devices()
        self.port = int(list(online - original_online)[0])
        self._emulator_launched = True

    def setup_port_forwarding(self, remote_port):
        """ Setup TCP port forwarding to the specified port on the device,
            using any availble local port, and return the local port.
        """

        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("",0))
        local_port = s.getsockname()[1]
        s.close()

        output = self._run_adb(['-s', 'emulator-%d' % self.port, 
                                'forward',
                                'tcp:%d' % local_port,
                                'tcp:%d' % remote_port])

        self.marionette_port = local_port

        return local_port

    def wait_for_port(self, timeout=180):
        assert(self.marionette_port)
        starttime = datetime.datetime.now()
        while datetime.datetime.now() - starttime < datetime.timedelta(seconds=timeout):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.connect(('localhost', self.marionette_port))
                data = sock.recv(10)
                sock.close()
                if '"from"' in data:
                    return True
            except:
                import traceback
                print traceback.format_exc()
            time.sleep(1)
        return False

