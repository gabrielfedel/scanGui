#!/usr/bin/python3
import collections
import csv
import yaml
import os.path
import re
import time
import atexit
import signal
import collections
from datetime import datetime
from glob import iglob
from sys import stderr

import docopt as docoptModule
from docopt import docopt, DocoptExit
from xdg.BaseDirectory import load_config_paths as loadConfigPaths
from xdg.BaseDirectory import xdg_config_dirs as xdgConfigDirs

from epics import PV
from py4syn import mtrDB
from py4syn.epics.Keithley6514Class import Keithley6514
from py4syn.epics.LinkamCI94Class import LinkamCI94
from py4syn.epics.MarCCDClass import MarCCD
from py4syn.epics.OmronE5CKClass import OmronE5CK
from py4syn.epics.PseudoCounterClass import PseudoCounter
from py4syn.epics.ScalerClass import Scaler
from py4syn.epics.SimCountableClass import SimCountable
from py4syn.epics.MotorClass import Motor
from py4syn.epics.ShutterClass import SimpleShutter, ToggleShutter
from py4syn.utils import counter
from py4syn.utils import motor
from py4syn.utils.scan import createUserDefinedDataField, ScanType, Scan
import py4syn.utils.scan as scanModule
from py4syn.epics.ICountable import ICountable
from py4syn.epics.LaudaClass import Lauda
from py4syn.epics.PilatusClass import Pilatus
from py4syn.epics.DxpClass import Dxp
from py4syn.epics.DxpFakeClass import DxpFake
from py4syn.epics.OceanClass import OceanOpticsSpectrometer

from .CountablePV import CountablePV
from .NullMotor import NullMotor

import importlib

validMotorName = re.compile(r'^\w+$')
counterName = re.compile(r'plotselected:\W*"(.*)"')

BASE_DIRECTORY = 'scan-utils'
CONSTANTS = 'constants.yml'

# Generator that returns dictionary with file data from all configuration directories
# from least priority to most
def configurationLoop(fileName, loader=yaml.load):
    basePath = os.path.join(BASE_DIRECTORY, fileName)
    paths = reversed([fileName] + list(loadConfigPaths(basePath)))

    for path in paths:
        try:
            with open(path) as f:
                yield loader(f)
        except FileNotFoundError:
            pass
        except Exception as e:
            print('Warning: unable to process configuration file "%s"\n'
                  '%s\nSkipping file...' % (path, e))

def loadConfiguration(fileName='config.yml', legacyMotors='motors.txt'):
    counters = {}
    motors = {}
    misc = {}

    for configuration in configurationLoop(legacyMotors, loader=parseSAXS2MotorFile):
        motors.update(configuration.get('motors') or {})

    for configuration in configurationLoop(fileName):
        counters.update(configuration.get('counters') or {})
        motors.update(configuration.get('motors') or {})
        misc.update(configuration.get('misc') or {})

    if len(counters) == 0:
        raise ValueError('No counter available in configuration')

    if len(motors) == 0:
        raise ValueError('No motor available in configuration')

    return {'counters': counters, 'motors': motors, 'misc': misc}

# Parse CSV file containing list of active motors. Each line contains either a real motor
# (we get the name and PV), or a pseudo motor (we get its complete operating description)
def parseSAXS2MotorFile(f):
    motors = {}

    r = csv.reader(f)
    for m in r:
        name = m[1]

        if m[0] == 'Real':
            pv = m[2]
            description = m[3]

            try:
                readback = m[6]
            except IndexError:
                readback = pv + '.RBV'

            motors[name] = {
                'pv': pv,
                'readback': readback,
                'description': description,
                'type': 'real',
            }

            continue

        i = 2
        while validMotorName.match(m[i]):
            i += 1

        dependencies = m[2:i]
        position = m[i]
        targets = dict(zip(m[i+1:-3:2], m[i+2:-3:2]))
        description = m[-3]

        motors[name] = {
            'description': description,
            'dependencies': dependencies,
            'position': position,
            'targets': targets,
            'type': 'pseudo',
        }

    return {'motors': motors}

# Configuration file contains the list of counters that the user wants to use
def readConfiguration(fileName):
    basePath = os.path.join(BASE_DIRECTORY, fileName)
    paths = [fileName] + list(loadConfigPaths(basePath))

    for path in paths:
        try:
            with open(path) as f:
                return yaml.load(f)
        except FileNotFoundError:
            pass
        except Exception as e:
            print('Warning: unable to process configuration file "%s"\n'
                  '%s\nSkipping file...' % (path, e))

    raise FileNotFoundError('Configuration file "%s" not found in '
                            'configuration search path:\n%s' %
                            (basePath, ['.'] + xdgConfigDirs))

# Create motor. If motor is a pseudo motor, also create the full hierarchy of pseudo
# and real motors, as necessary.
def createMotor(name, configuration):
    motorMap = configuration['motors']

    # Motor already created, just return
    if name in mtrDB:
        return mtrDB[name]

    if not name in motorMap:
        raise ValueError('Motor not found: %s' % name)

    info = motorMap[name]

    if info['type'] == 'real':
        mtrDB[name] = MotorWithReadBack(name, info['pv'], info.get('readback', info['pv']+'.RBV'))
        return mtrDB[name]
    elif info['type'] == 'e5ck':
        mtrDB[name] = OmronE5CK(info['pv'], name)
        return mtrDB[name]
    elif info['type'] == 'ci94':
        linkam = LinkamCI94(info['pv'], name)
        linkam.setPumpSpeed(3)
        mtrDB[name] = linkam
        return linkam
    elif info['type'] == 'lauda':
        mtrDB[name] = Lauda(info['pv'], name)
        return mtrDB[name]
    elif info['type'] == 'null':
        mtrDB[name] = NullMotor(name)
        return mtrDB[name]

    # Create this pseudo motor first, dependencies later, to avoid infinite recursion
    motor.createPseudoMotor(name, info['description'], info['position'],
                            info['targets'])

    for m in info['targets']:
        createMotor(m, configuration)

    for m in info['dependencies']:
        createMotor(m, configuration)

    return mtrDB[name]

# Find the last "plotselected" line in a scan log file and return the counter name
def findActiveCounterFromSAXS2ScanFile(scanFilePattern):
    fileName = time.strftime(scanFilePattern)

    with open(fileName) as f:
        s = f.readlines()
        s.reverse()

    for l in s:
        m = counterName.search(l)
        if m:
            return m.group(1)

    raise LookupError('Error: no current active counter found.\nThe plotselect utility '
                      'may be used to define an active counter.')

MAX_NUM_CHANNELS = 20

def scalerBuilder(info, mnemonic):
    return Scaler(info['pv'], MAX_NUM_CHANNELS, mnemonic)

def keithleyDestructor(keithley, integration, average, averageType, averageCount,
                       continuous):
    keithley.setIntegrationTime(integration)
    keithley.setAverageDigitalFilter(average)
    keithley.setAverageCount(averageCount)
    keithley.setAverageTControl(averageType)
    keithley.setStatusContinuesMode(continuous)

def safeKill(signal, frame):
    raise SystemExit(-2)

def installSignalHandlers():
    signal.signal(signal.SIGHUP, safeKill)
    signal.signal(signal.SIGQUIT, safeKill)
    signal.signal(signal.SIGTERM, safeKill)

def keithleyBuilder(info, mnemonic):
    keithley = Keithley6514(info['pv'], mnemonic, timeBased=info.get('time-based', True))

    # Save keithley parameters and restore them on program exit
    integration = keithley.getIntegrationTime()
    average = keithley.getAverageDigitalFilter()
    averageType = keithley.getAverageTControl()
    averageCount = keithley.getAverageCount()
    continuous = keithley.getStatusContinuesMode()
    atexit.register(keithleyDestructor, keithley, integration, average, averageType,
                    averageCount, continuous)
    installSignalHandlers()
    keithley.setStatusContinuesMode(0)

    return keithley

def processUserField(counter):
    now = datetime.now()

    if counter['type'] == 'date':
        format = counter.get('format', '%Y-%m-%d')
        return now.strftime(format)
    elif counter['type'] == 'time':
        format = counter.get('format', '%H:%M:%S.%f')
        return now.strftime(format)

def userDefinedBuilder(info, mnemonic):
    createUserDefinedDataField(mnemonic)

def cameraBuilder(info, mnemonic):
    camera = MarCCD(mnemonic, (info['ip'], int(info['address'])))
    atexit.register(lambda: camera.close())
    installSignalHandlers()

    return camera

def pilatusBuilder(info, mnemonic):
    p = Pilatus(mnemonic, info['pv'])
    atexit.register(p.close)
    installSignalHandlers()

    return p

DXP_MAX_NUM_CHANNELS = 4
DXP_MAX_NUM_ROIS_PER_CHANNEL = 32

def dxpBuilder(info, mnemonic, out='out'):
    d = Dxp(mnemonic,DXP_MAX_NUM_CHANNELS,DXP_MAX_NUM_ROIS_PER_CHANNEL,info['pv'],output=out)
    atexit.register(d.close)
    #installSignalHandlers()

    return d

def dxpFakeBuilder(info, mnemonic, out='out'):
    d = DxpFake(mnemonic,DXP_MAX_NUM_CHANNELS,DXP_MAX_NUM_ROIS_PER_CHANNEL,info['pv'],output=out)
    atexit.register(d.close)
    #installSignalHandlers()

    return d

def qe65000Builder(info, mnemonic, out='out'):
    q = OceanOpticsSpectrometer(mnemonic, info['pv'], output=out)
    atexit.register(q.close)
    #installSignalHandlers()

    return q

def isCamera(type):
    return type == 'marccd'

counterBuilder = {
    'scaler': scalerBuilder,
    'keithley': keithleyBuilder,
    'pv': lambda info, name: CountablePV(info['pv'], name),
    'virtual': lambda info, name: SimCountable(info['pv'], name),
    'date': userDefinedBuilder,
    'time': userDefinedBuilder,
    'math': lambda info, name: PseudoCounter(name, info['formula']),
    'marccd': cameraBuilder,
    'pilatus': pilatusBuilder,
    'dxp': dxpBuilder,
    'dxpfake' :dxpFakeBuilder,
    'qe65000' :qe65000Builder
}

# Create counters using counterMap for configuration
def createCounters(counters, configuration, output=None):
    counterMap = configuration['counters']
    devices = {}

    # clear the old counters
    if counter.getActiveCountersNumber() != 0:
        counter.clearCounterDB()
    for name in counters:
        try:
            info = counterMap[name]

            # "scanfile" counters actually store the selected counter in another file
            if 'scanfile' in info:
                name = findActiveCounterFromSAXS2ScanFile(info['scanfile'])
                info = counterMap[name]
        except KeyError:
            raise ValueError('Counter not found: %s' % name) from None

        pv = info.get('pv', None)
        channel = info.get('channel', None)
        monitor = info.get('monitor', False)
        factor = info.get('factor', 1)
        ip = info.get('ip', None)
        address = info.get('address', None)
        formula = info.get('formula', None)
        spectra = info.get('spectra', False)
        print('spectra ', spectra)

        try:
            type = info['type']
        except KeyError:
            raise ValueError('Counter %s doesn\'t have a type field' % name) from None

        if (type, pv, ip, address, formula, spectra) in devices:
            device = devices[type, pv, ip, address, formula, spectra]
        else:
            try:
                if (output is not None) and (type == 'dxp' or type == 'dxpfake'
                        or type == "qe65000"):
                    device = counterBuilder[type](info, name, output)
                else:
                    device = counterBuilder[type](info, name)
                devices[type, pv, ip, address, formula, spectra] = device
            except KeyError:
                raise ValueError('Unable to build device with type %s' % type) from None

        # The created counter may not be an ICountable (ex: date, time, etc.)
        if(isinstance(device, ICountable)):
            counter.createCounter(name, device, channel, monitor, factor)
    return devices

def createCameraShutters(counters, configuration):
    shutters = {}

    for name in counters:
        try:
            info = configuration['counters'][name]
        except KeyError:
            raise ValueError('Counter not found: %s' % name) from None

        shutter = info.get('shutter')

        if shutter is None or shutter in shutters:
            continue

        type = info.get('shutter-type')
        if type == 'toggle':
            shutters[shutter] = ToggleShutter(shutter, shutter, info['shutter-readback'])
        elif type == 'simple':
            shutters[shutter] = SimpleShutter(shutter, shutter)
        else:
            raise ValueError('Unknown shutter type: %s' % type)

        atexit.register(shutters[shutter].close)

    return tuple(shutters.values())

class MotorWithReadBack(Motor):
    '''A motor class with separate read back value PV'''
    def __init__(self, mnemonic, pvName, readBackName):
        super().__init__(pvName, mnemonic)
        self.readBack = PV(readBackName)

    def getRealPosition(self):
        return self.readBack.get()

def die(*v):
    print(*v, file=stderr)
    raise SystemExit(-1)

def listSearchPaths():
    print('Configuration search paths...')
    dirs = ['.'] + [os.path.join(x, BASE_DIRECTORY) for x in xdgConfigDirs]
    print(dirs)
    print()

    return dirs

def listCounterSets(dirs):
    print('Listing all known configurations...')

    for dir in dirs:
        for name in iglob(os.path.join(dir, 'config.*.yml')):
            c = readConfiguration(name)
            print('- %s: [%s]' % (os.path.basename(name)[7:-4], name))
            print('\n'.join('\t- %s' % x for x in c))
    print()

def listCounters(configuration):
    print('Listing all known counters...')
    counters = configuration['counters']
    for name in sorted(counters):
        counter = counters[name]
        d = counter.get('description', '')

        if counter['type'] == 'scaler' or counter['type'] == 'virtual':
            s = '- %s: %s.S%d [%s]' % (name, counter['pv'], counter['channel'], d)
        elif counter['type'] == 'pv' or counter['type'] == 'keithley':
            s = '- %s: %s [%s]' % (name, counter['pv'], d)
        elif counter['type'] == 'marccd':
            s = '- %s: %s:%d [%s]' % (name, counter['ip'], counter['address'], d)
        else:
            s = '- %s: [%s]' % (name, d)

        print(s)
    print()

def listMotors(configuration):
    print('Listing all known motors...')
    motors = configuration['motors']
    for name in sorted(motors):
        m = motors[name]
        print('%s: %s [%s]' % (name, m.get('pv') or 'pseudo', m.get('description', '')))

def listConfigurations():
    dirs = listSearchPaths()
    listCounterSets(dirs)
    configuration = loadConfiguration()
    listCounters(configuration)
    listMotors(configuration)

def loadConstants():
    basePath = os.path.join(BASE_DIRECTORY, CONSTANTS)
    paths = [CONSTANTS] + list(loadConfigPaths(basePath))

    for path in paths:
        try:
            with open(path) as f:
                return yaml.load(f), path
        except:
            pass

    return {}, None

def saveConstants(constants, path):
    if path is not None:
        paths = [path]
    else:
        basePath = os.path.join(BASE_DIRECTORY, CONSTANTS)
        paths = reversed([fileName] + list(loadConfigPaths(basePath)))

    for path in paths:
        print(path)
        with open(path, 'w') as f:
            yaml.dump(constants, f, default_flow_style=False)
            return

    raise RuntimeError('Unable to save constants file')

# Extend py4syn scan to support multiple sub scans. The difference is that
# the calls to __launchCounters, __operationCallback and __saveCounterData are
# replaced by a loop calling subScanCallback. Also, waitComplete, a subset of
# the original __saveCounterData was added.
class SubScan(Scan):
    def __init__(self, count, callback, *args, **kwargs):
        super().__init__(ScanType.SCAN, *args, **kwargs)
        self.subScanCount = count
        self.subScanCallback = callback

    def waitComplete(self, idxs):
        t = self.getCountTime()

        if isinstance(t, collections.Iterable):
            t = t[int(idxs[-1])]

        if(t < 0):
            counter.waitAll(monitor=True)
        else:
            counter.waitAll(monitor=False)

        counter.stopAll()

    def doScan(self):
        # Arrays to store positions and indexes to be used as callback arguments
        positions = []
        indexes = []

        # Pre Scan Callback
        if(self._Scan__preScanCallback):
            self._Scan__preScanCallback(scan=self)

        for pointIdx in range(0, self.getNumberOfPoints()):
            # Saves point index at SCAN_DATA
            scanModule.SCAN_DATA['points'].append(pointIdx)

            # Pre Point Callback
            if(self._Scan__prePointCallback):
                self._Scan__prePointCallback(scan=self, pos=positions, idx=indexes)

            self._Scan__waitDelay(scan=self, pos=positions, idx=indexes)

            for deviceIdx in range(0, self.getNumberOfParams()):
                param = self.getScanParams()[deviceIdx]
                param.getDevice().setValue(param.getPoints()[pointIdx])
                indexes.append(pointIdx)

            self._Scan__waitDevices()

            for deviceIdx in range(0, self.getNumberOfParams()):
                param = self.getScanParams()[deviceIdx]
                positions.append(param.getDevice().getValue())
                # Saves device position at SCAN_DATA
                scanModule.SCAN_DATA[param.getDevice().getMnemonic()].append(param.getDevice().getValue())

            # Pre Operation Callback
            if(self._Scan__preOperationCallback):
                self._Scan__preOperationCallback(scan=self, pos=positions, idx=indexes)

            for i in range(self.subScanCount):
                self.subScanCallback(scan=self, pos=positions, idx=indexes, sub=i)

            # Post Operation Callback
            if(self._Scan__postOperationCallback):
                self._Scan__postOperationCallback(scan=self, pos=positions, idx=indexes)

            self._Scan__writeData(idx=pointIdx)

            # Updates the screen and plotter
            self._Scan__printAndPlot()

            # Post Point Callback
            if(self._Scan__postPointCallback):
                self._Scan__postPointCallback(scan=self, pos=positions, idx=indexes)

        # Post Scan Callback
        if(self._Scan__postScanCallback):
            self._Scan__postScanCallback(scan=self)

# Same as docopt parse_argv function, but doesn't consider negative numbers
# as parameters
def docoptParseArgvWithNegativeNumbers(tokens, options, options_first=False):
    parsed = []
    while tokens.current() is not None:
        if tokens.current() == '--':
            return parsed + [docoptModule.Argument(None, v) for v in tokens]
        elif tokens.current().startswith('--'):
            parsed += docoptModule.parse_long(tokens, options)
        elif tokens.current().startswith('-') and tokens.current() != '-':
            try:
                f = float(tokens.current())
                parsed.append(docoptModule.Argument(None, tokens.move()))
            except ValueError:
                parsed += docoptModule.parse_shorts(tokens, options)
        elif options_first:
            return parsed + [docoptModule.Argument(None, v) for v in tokens]
        else:
            parsed.append(docoptModule.Argument(None, tokens.move()))
    return parsed

docoptModule.parse_argv = docoptParseArgvWithNegativeNumbers
