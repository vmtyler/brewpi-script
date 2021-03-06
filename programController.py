# Copyright 2012 BrewPi/Elco Jacobs.
# This file is part of BrewPi.

# BrewPi is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# BrewPi is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with BrewPi.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import print_function
import subprocess as sub
import time
import simplejson as json
import os
import brewpiVersion
import expandLogMessage
from MigrateSettings import MigrateSettings
from sys import stderr
import BrewPiUtil as util
import subprocess
import platform
import sys

# print everything in this file to stderr so it ends up in the correct log file for the web UI
def printStdErr(*objs):
    print(*objs, file=stderr)

def asbyte(v):
    return chr(v & 0xFF)

def waitForReset(wait_time):
    printStdErr("Waiting 15 seconds for device to reset and come back as serial port.")
    for i in range(wait_time + 1):
        time.sleep(1)
        sys.stderr.write("{0}/15 \r".format(i), ) # overwrite same line
    printStdErr("") # print newline

class LightYModem:
    """
    Receive_Packet
    - first byte SOH/STX (for 128/1024 byte size packets)
    - EOT (end)
    - CA CA abort
    - ABORT1 or ABORT2 is abort

    Then 2 bytes for seqno (although the sequence number isn't checked)

    Then the packet data

    Then CRC16?

    First packet sent is a filename packet:
    - zero-terminated filename
    - file size (ascii) followed by space?
    """

    soh = 1  # 128 byte blocks
    stx = 2  # 1K blocks
    eot = 4
    ack = 6
    nak = 0x15
    ca = 0x18  # 24
    crc16 = 0x43  # 67  'C
    abort1 = 0x41  # 65  'A'
    abort2 = 0x61  # 97 'a'

    packet_len = 128
    expected_packet_len = packet_len + 5
    packet_mark = soh

    def __init__(self):
        self.seq = None
        self.ymodem = None

    def _read_response(self):
        ch1 = ''
        while not ch1:
            ch1 = self.ymodem.read(1)
        ch1 = ord(ch1)
        if ch1==LightYModem.ack and self.seq==0:    # may send also a crc16
            ch2 = self.ymodem.read(1)
        elif ch1==LightYModem.ca:                   # cancel, always sent in pairs
            ch2 = self.ymodem.read(1)
        return ch1

    def _send_ymodem_packet(self, data):
        # pad string to 1024 chars
        data = data.ljust(LightYModem.packet_len)
        seqchr = asbyte(self.seq & 0xFF)
        seqchr_neg = asbyte((-self.seq-1) & 0xFF)
        crc16 = b'\x00\x00'
        packet = asbyte(LightYModem.packet_mark) + seqchr + seqchr_neg + data + crc16
        if len(packet)!= LightYModem.expected_packet_len:
            raise Exception("packet length is wrong!")

        self.ymodem.write(packet)
        self.ymodem.flush()
        response = self._read_response()
        if response==LightYModem.ack:
            printStdErr("sent packet nr %d " % (self.seq))
            self.seq += 1
        return response

    def _send_close(self):
        self.ymodem.write(asbyte(LightYModem.eot))
        self.ymodem.flush()
        response = self._read_response()
        if response == LightYModem.ack:
            self.send_filename_header("", 0)
            self.ymodem.close()

    def send_packet(self, file, output):
        response = LightYModem.eot
        data = file.read(LightYModem.packet_len)
        if len(data):
            response = self._send_ymodem_packet(data)
        return response

    def send_filename_header(self, name, size):
        self.seq = 0
        packet = name + asbyte(0) + str(size) + ' '
        return self._send_ymodem_packet(packet)

    def transfer(self, file, ymodem, output):
        self.ymodem = ymodem
        """
        file: the file to transfer via ymodem
        ymodem: the ymodem endpoint (a file-like object supporting write)
        output: a stream for output messages
        """
        file.seek(0, os.SEEK_END)
        size = file.tell()
        file.seek(0, os.SEEK_SET)
        response = self.send_filename_header("binary", size)
        while response==LightYModem.ack:
            response = self.send_packet(file, output)

        file.close()
        if response==LightYModem.eot:
            self._send_close()

        return response


def programController(config, boardType, hexFile, system1File, system2File, useDfu, restoreWhat):
    programmer = SerialProgrammer.create(config, boardType)
    if programmer is None:
        printStdErr("Couldn't detect a compatible board to program")
        return 0
    return programmer.program(hexFile, system1File, system2File, useDfu, restoreWhat)


def json_decode_response(line):
    try:
        return json.loads(line[2:])
    except json.decoder.JSONDecodeError, e:
        printStdErr("JSON decode error: " + str(e))
        printStdErr("Line received was: " + line)

msg_map = { "a" : "unknown board type" }

class SerialProgrammer:
    @staticmethod
    def create(config, boardType):
        if boardType=='core':
            msg_map["a"] = "Spark Core"
            programmer = SparkProgrammer(config, boardType)
        elif boardType == 'photon':
            msg_map["a"] = "Photon"
            programmer = SparkProgrammer(config, boardType)
        elif boardType == 'p1':
            msg_map["a"] = "P1"
            programmer = SparkProgrammer(config, boardType)
        else:
            programmer = None
        return programmer

    def __init__(self, config):
        self.config = config
        self.restoreSettings = False
        self.restoreDevices = False
        self.ser = None
        self.versionNew = None
        self.versionOld = None
        self.oldSettings = {}

    def program(self, hexFile, system1File, system2File, useDfu, restoreWhat):
        printStdErr("****    %(a)s Program script started    ****" % msg_map)

        self.parse_restore_settings(restoreWhat)

        if self.restoreSettings or self.restoreDevices:
            printStdErr("Checking old version before programming.")
            if not self.open_serial(self.config, 57600, 0.2):
                return 0
            self.delay_serial_open()
            # request all settings from board before programming
            if self.fetch_current_version():
                self.retrieve_settings_from_serial()
                self.save_settings_to_file()

        running_as_root = False
        try:
            running_as_root = os.getuid() == 0
        except AttributeError:
            pass # not running on Linux, use serial
        if running_as_root and self.boardType == "photon":
            # default to DFU mode when possible on the Photon. Serial is not always stable
            printStdErr("\nFound a Photon and running as root/sudo, using DFU mode to flash firmware.")
            useDfu = True

        if useDfu:
            printStdErr("\nTrying to automatically reboot into DFU mode and update your firmware.")
            printStdErr("\nIf the Photon does not reboot into DFU mode automatically, please put it in DFU mode manually.")

            if self.ser:
                self.ser.close()

            myDir = os.path.dirname(os.path.abspath(__file__))
            flashDfuPath = os.path.join(myDir, 'utils', 'flashDfu.py')
            command = sys.executable + ' ' + flashDfuPath + " --autodfu --noreset --file={0}".format(os.path.abspath(hexFile))
            if system1File is not None and system2File is not None:
                systemParameters = " --system1={0} --system2={1}".format(system1File, system2File)
                command = command + systemParameters
            if platform.system() == "Linux":
                command =  'sudo ' + command
            printStdErr("Running command: " + command)
            process = subprocess.Popen(command, shell=True)
            process.wait()

            printStdErr("\nUpdating firmware over DFU finished\n")

        else:
            if not self.ser:
                if not self.open_serial(self.config, 57600, 0.2):
                    printStdErr("Could not open serial port to flash the firmware.")
                    return 0
            self.delay_serial_open()
            if system1File:
                printStdErr("Flashing system part 1.")
                if not self.flash_file(system1File):
                    return 0

                if not self.open_serial_with_retry(self.config, 57600, 0.2):
                    printStdErr("Error opening serial port after flashing system part 1. Program script will exit.")
                    printStdErr("If your device stopped working, use flashDfu.py to restore it.")
                    return False

            if system2File:
                printStdErr("Flashing system part 2.")
                if not self.flash_file(system2File):
                    return 0

                if not self.open_serial_with_retry(self.config, 57600, 0.2):
                    printStdErr("Error opening serial port after flashing system part 2. Program script will exit.")
                    printStdErr("If your device stopped working, use flashDfu.py to restore it.")
                    return False

            if(hexFile):
                if not self.flash_file(hexFile):
                    return 0

                if not self.open_serial_with_retry(self.config, 57600, 0.2):
                    printStdErr("Error opening serial port after flashing user part. Program script will exit.")
                    printStdErr("If your device stopped working, use flashDfu.py to restore it.")
                    return False

        if self.ser:
            self.ser.close
            self.ser = None

        waitForReset(15)
        printStdErr("Now checking new version.")
        if not self.open_serial_with_retry(self.config, 57600, 0.2):
            printStdErr("Error opening serial port after programming. Program script will exit. Settings are not restored.")
            printStdErr("If your device stopped working, use flashDfu.py to restore it.")
            return False

        self.fetch_new_version()
        self.reset_settings()
        if self.restoreSettings or self.restoreDevices:
            printStdErr("Now checking which settings and devices can be restored...")
        if self.versionNew is None:
            printStdErr(("Warning: Cannot receive version number from controller after programming. "
                         "\nSomething must have gone wrong. Restoring settings/devices settings failed.\n"))
            return 0

        if not self.versionOld and (self.restoreSettings or self.restoreDevices):
            printStdErr("Could not receive valid version number from old board, " +
                        "No settings/devices are restored.")
            return 0

        if self.restoreSettings:
            printStdErr("Trying to restore compatible settings from " +
                        self.versionOld.toString() + " to " + self.versionNew.toString())

            if(self.versionNew.isNewer("0.2")):
                printStdErr("Sorry, settings can only be restored when updating to BrewPi 0.2.0 or higher")
                self.restoreSettings = False

        if self.restoreSettings:
            self.restore_settings()

        if self.restoreDevices:
            self.restore_devices()

        printStdErr("****    Program script done!    ****")
        self.ser.close()
        self.ser = None
        return 1

    def parse_restore_settings(self, restoreWhat):
        restoreSettings = False
        restoreDevices = False
        if 'settings' in restoreWhat:
            if restoreWhat['settings']:
                restoreSettings = True
        if 'devices' in restoreWhat:
            if restoreWhat['devices']:
                restoreDevices = True
        # Even when restoreSettings and restoreDevices are set to True here,
        # they might be set to false due to version incompatibility later

        printStdErr("Settings will " + ("" if restoreSettings else "not ") + "be restored" +
                    (" if possible" if restoreSettings else ""))
        printStdErr("Devices will " + ("" if restoreDevices else "not ") + "be restored" +
                    (" if possible" if restoreSettings else ""))
        self.restoreSettings = restoreSettings
        self.restoreDevices = restoreDevices

    def open_serial(self, config, baud, timeout):
        if self.ser:
            self.ser.close()
        self.ser = None
        self.ser = util.setupSerial(config, baud, timeout)
        if self.ser is None:
            return False
        return True

    def open_serial_with_retry(self, config, baud, timeout):
        # reopen serial port
        retries = 30
        self.ser = None
        while retries:
            time.sleep(1)
            if self.open_serial(config, baud, timeout):
                return True
            retries -= 1
        return False

    def delay_serial_open(self):
        pass

    def fetch_version(self, msg):
        version = brewpiVersion.getVersionFromSerial(self.ser)
        if version is None:
            printStdErr("Warning: Cannot receive version number from controller. " +
                        "Your controller is either not programmed yet or running a very old version of BrewPi. " +
                        "It will be reset to defaults.")
        else:
            printStdErr(msg+"Found " + version.toExtendedString() +
                        " on port " + self.ser.name + "\n")
        return version

    def fetch_current_version(self):
        self.versionOld = self.fetch_version("Checking current version: ")
        return self.versionOld

    def fetch_new_version(self):
        self.versionNew = self.fetch_version("Checking new version: ")
        return self.versionNew

    def retrieve_settings_from_serial(self):
        ser = self.ser
        self.oldSettings.clear()
        printStdErr("Requesting old settings from %(a)s..." % msg_map)
        expected_responses = 2
        if not self.versionOld.isNewer("0.2.0"):  # versions older than 2.0.0 did not have a device manager
            expected_responses += 1
            ser.write("d{}\n")  # installed devices
            time.sleep(1)
        ser.write("c\n")  # control constants
        ser.write("s\n")  # control settings
        time.sleep(2)

        while expected_responses:
            line = ser.readline()
            if line:
                line = util.asciiToUnicode(line)
                if line[0] == 'C':
                    expected_responses -= 1
                    self.oldSettings['controlConstants'] = json_decode_response(line)
                elif line[0] == 'S':
                    expected_responses -= 1
                    self.oldSettings['controlSettings'] = json_decode_response(line)
                elif line[0] == 'd':
                    expected_responses -= 1
                    self.oldSettings['installedDevices'] = json_decode_response(line)


    def save_settings_to_file(self):
        oldSettingsFileName = 'settings-' + time.strftime("%b-%d-%Y-%H-%M-%S") + '.json'
        settingsBackupDir = util.scriptPath() + '/settings/controller-backup/'
        if not os.path.exists(settingsBackupDir):
            os.makedirs(settingsBackupDir, 0777)

        oldSettingsFilePath = os.path.join(settingsBackupDir, oldSettingsFileName)
        oldSettingsFile = open(oldSettingsFilePath, 'wb')
        oldSettingsFile.write(json.dumps(self.oldSettings))
        oldSettingsFile.truncate()
        oldSettingsFile.close()
        os.chmod(oldSettingsFilePath, 0777) # make sure file can be accessed by all in case the script ran as root
        printStdErr("Saved old settings to file " + oldSettingsFileName)

    def delay(self, countDown):
        while countDown > 0:
            time.sleep(1)
            countDown -= 1
            printStdErr("Back up in " + str(countDown) + "...")

    def flash_file(self, hexFile):
        raise Exception("not implemented")

    def reset_settings(self, setTestMode = False):
        printStdErr("Resetting EEPROM to default settings")
        self.ser.write('E\n')
        if setTestMode:
            self.ser.write('j{mode:t}\n')
        time.sleep(5)  # resetting EEPROM takes a while, wait 5 seconds
        # read log messages from controller
        while 1:  # read all lines on serial interface
            line = self.ser.readline()
            if line:  # line available?
                if line[0] == 'D':
                    self.print_debug_log(line)
            else:
                break

    def print_debug_log(self, line):
        try:  # debug message received
            expandedMessage = expandLogMessage.expandLogMessage(line[2:])
            printStdErr(expandedMessage)
        except Exception, e:  # catch all exceptions, because out of date file could cause errors
            printStdErr("Error while expanding log message: " + str(e))
            printStdErr(("%(a)s debug message: " % msg_map) + line[2:])

    def restore_settings(self):
        oldSettingsDict = self.get_combined_settings_dict(self.oldSettings)
        ms = MigrateSettings()
        restored, omitted = ms.getKeyValuePairs(oldSettingsDict,
                                                self.versionOld.toString(),
                                                self.versionNew.toString())

        printStdErr("Migrating these settings: " + json.dumps(restored.items()))
        printStdErr("Omitting these settings: " + json.dumps(omitted.items()))

        self.send_restored_settings(restored)


    def get_combined_settings_dict(self, oldSettings):
        combined = oldSettings.get('controlConstants').copy() # copy keys/values from controlConstants
        combined.update(oldSettings.get('controlSettings')) # add keys/values from controlSettings
        return combined

    def send_restored_settings(self, restoredSettings):
        for key in restoredSettings:
            setting =  restoredSettings[key]
            command = "j{" + json.dumps(key) + ":" + json.dumps(setting) + "}\n"
            self.ser.write(command)
            # make readline blocking for max 5 seconds to give the controller time to respond after every setting
            oldTimeout = self.ser.timeout
            self.ser.timeout = 5
            # read all replies
            while 1:
                line = self.ser.readline()
                if line:  # line available?
                    if line[0] == 'D':
                        self.print_debug_log(line)
                if self.ser.inWaiting() == 0:
                    break
            self.ser.timeout = 5

    def restore_devices(self):
        ser = self.ser

        oldDevices = self.oldSettings.get('installedDevices')
        if oldDevices:
            printStdErr("Now trying to restore previously installed devices: " + str(oldDevices))
        else:
            printStdErr("No devices to restore!")
            return

        detectedDevices = None
        for device in oldDevices:
            printStdErr("Restoring device: " + json.dumps(device))
            if "a" in device.keys(): # check for sensors configured as first on bus
                if int(device['a'], 16) == 0:
                    printStdErr("OneWire sensor was configured to autodetect the first sensor on the bus, " +
                                "but this is no longer supported. " +
                                "We'll attempt to automatically find the address and add the sensor based on its address")
                    if detectedDevices is None:
                        ser.write("h{}\n")  # installed devices
                        time.sleep(1)
                        # get list of detected devices
                        for line in ser:
                            if line[0] == 'h':
                                detectedDevices = json_decode_response(line)

                    for detectedDevice in detectedDevices:
                        if device['p'] == detectedDevice['p']:
                            device['a'] = detectedDevice['a'] # get address from sensor that was first on bus

            ser.write("U" + json.dumps(device) + "\n")

            requestTime = time.time()
            # read log messages from controller
            while 1:  # read all lines on serial interface
                line = ser.readline()
                if line:  # line available?
                    if line[0] == 'D':
                        self.print_debug_log(line)
                    elif line[0] == 'U':
                        printStdErr(("%(a)s reports: device updated to: " % msg_map) + line[2:])
                        break
                if time.time() > requestTime + 5:  # wait max 5 seconds for an answer
                    break
        printStdErr("Restoring installed devices done!")


class SparkProgrammer(SerialProgrammer):
    def __init__(self, config, boardType):
        SerialProgrammer.__init__(self, config)
        self.boardType = boardType

    def flash_file(self, hexFile):
        printStdErr("Triggering a firmware update with the ymodem protocol on the controller")
        self.ser.write("F\n")
        time.sleep(1)
        printStdErr("Flashing file {0}".format(hexFile))
        file = open(hexFile, 'rb')
        result = LightYModem().transfer(file, self.ser, stderr)
        file.close()
        success = result==LightYModem.eot
        printStdErr("File flashed successfully" if success else "Problem flashing file: " + str(result) +
                                                                "\nPlease try again.")
        return success
