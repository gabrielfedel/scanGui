from os import unlink
from shutil import move
from scipy.misc import imread, toimage
import numpy
from py4syn import counterDB
from PIL import Image

from . import helpers
from .CountablePV import CountablePV
from .PilatusClass import Pilatus

from py4syn.epics.Keithley6514Class import Keithley6514
from py4syn.epics.MarCCDClass import MarCCD
from py4syn.utils.counter import ctr, getCountersData
import py4syn.utils.scan as scanModule

class ScanPipeline(helpers.SubScan):
    '''Implements complex pipeline to support acquisition with cameras,
    in particular MarCCD cameras, which require multiple step (acquire, dezinger,
    write file, etc.) acquisition.'''

    SUFFIX1 = '-step1'
    SUFFIX2 = '-step2'

    def __init__(self, devices, shutters, imageNames = {}):
        super().__init__(len(self.STEP_FN), self.pipelineStep)
        self.splitDeviceList(devices)
        self.shutters = shutters
        self.imageNames = imageNames

        if len(self.marccd) > 0:
            self.counts = 2
        else:
            self.counts = 1

    def splitDeviceList(self, devices):
        accumulators = []
        integrators = []
        marccd = []
        simpleCameras = []

        for d in devices:
            if isinstance(d, Keithley6514) or isinstance(d, CountablePV):
                integrators.append(d)
            elif isinstance(d, MarCCD):
                marccd.append(d)
            elif isinstance(d, Pilatus):
                simpleCameras.append(d)
            else:
                accumulators.append(d)

        self.accumulators = tuple(accumulators)
        self.integrators = tuple(integrators)
        self.marccd = tuple(marccd)
        self.simpleCameras = tuple(simpleCameras)

    def pipelineStep(self, pos, idx, sub, **kwargs):
        self.STEP_FN[sub](self, pos, idx)

    def count1(self, positions, indexes):
        self.t = self.getCountTime()[indexes[-1]]

        if self.counts == 2:
            self.t /= 2

        for c in self.simpleCameras:
            name = self.imageNames[c.getMnemonic()][indexes[-1]] + self.SUFFIX1
            c.setImageName(name)

        ctr(abs(self.t), use_monitor=self.t<0, wait=False)

        # Shutter is open after count has started to avoid artifacts in MarCCD image
        # This will effectivelly reduce the actual acquisition time, because the
        # shutter may take dozens of milliseconds to open.
        for shutter in self.shutters:
            shutter.open()

        self.waitComplete(indexes)

        for shutter in self.shutters:
            shutter.close()

        self.count1Data = getCountersData()

    def breathe(self, positions, indexes):
        for m in self.marccd:
            m.waitForIdle()

    def count2(self, positions, indexes):
        if self.counts != 2:
            return

        for c in self.simpleCameras:
            name = self.imageNames[c.getMnemonic()][indexes[-1]] + self.SUFFIX2
            c.setImageName(name)

        ctr(abs(self.t), use_monitor=self.t<0, wait=False)

        # Shutter is open after count has started to avoid artifacts in MarCCD image
        # This will effectivelly reduce the actual acquisition time, because the
        # shutter may take dozens of milliseconds to open.
        for shutter in self.shutters:
            shutter.open()

        self.waitComplete(indexes)

        for shutter in self.shutters:
            shutter.close()

        self.count2Data = getCountersData()

    def mergeImages(self, device, index):
        target = self.imageNames[device.getMnemonic()][index]
        n1 = target + self.SUFFIX1
        n2 = target + self.SUFFIX2
        i1 = imread(n1)
        i2 = imread(n2)
        i = i1+i2
        s = numpy.sum(i)
        ii = Image.frombytes('I', (i.shape[1], i.shape[0]), i.tostring())
        ii.save(target)
        try:
            unlink(n1)
            unlink(n2)
        except:
            print('Warning: unable to delete temporary images: %s, %s' % (n1, n2))

        return s

    def merge(self, positions, indexes):
        if self.counts == 1:
            for k, v in self.count1Data.items():
                scanModule.SCAN_DATA[k].append(v)
            for c in self.simpleCameras:
                target = self.imageNames[c.getMnemonic()][indexes[-1]]
                name = target + self.SUFFIX1
                move(name, target)

            return

        mergeMap = {
            self.accumulators: lambda d, x, y: x+y,
            self.integrators: lambda d, x, y: (x+y)/2,
            self.marccd: lambda d, x, y: d.dezinger() or 0,
            self.simpleCameras: lambda d, x, y: self.mergeImages(d, indexes[-1]),
        }

        for l, fn in mergeMap.items():
            for dev in l:
                for c in counterDB:
                    if counterDB[c]['device'] != dev:
                        continue
                    name = dev.getMnemonic()
                    v = fn(dev, self.count1Data[c], self.count2Data[c])
                    scanModule.SCAN_DATA[c].append(v)

    def correct(self, positions, indexes):
        for m in self.marccd:
            m.correct()

    def write(self, positions, indexes):
        for m in self.marccd:
            name = m.getMnemonic()
            t = self.imageNames[name][indexes[-1]]
            m.writeImage(t, wait=False)

        for m in self.marccd:
            m.waitForImage()

    STEP_FN = [count1, breathe, count2, merge, correct, write]
