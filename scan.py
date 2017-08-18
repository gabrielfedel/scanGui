#!/usr/bin/env python3
"""Perform a scan using specified motor and some list of counters provided in
the configuration file

Usage:
    scan [-r | -a] [-c <config>] [--optimum <counter-target>] [-o <fileprefix>] [-s] [-m <text>] [--count <n>] [--sleep <n>] [--] <motor> (<initial> <final> <step-or-count> <acquisition-time>)...
    scan -x [-r | -a] [-c <config>] [--optimum <counter-target>] [-o <outputdir>]
    [-s] [-m <text>] [--count <n>] [--sleep <n>] [--] [--time <acquisition-time>] (<motor> <initial> <final> <steps>)...
    scan -l
    scan -h

Options:
    -r, --relative      Do a relative scan and then return to initial position
    -a, --absolute      Do an absolute scan
    -c <config>, --configuration=<config>
                        Choose a counter configuration file [default: default]
    --optimum=<counter-target>
                        Move motor to the optimal point according to this
                        counter after scan
    -l, --list-configurations
                        List configurations instead of scanning
    -m <text>, --message=<text>
                        String of comments to put in output file header
    --count <n>         Scan multiple times [default: 1]
    --sleep <n>         Sleep time before each acquisition [default: 0]
    -o <fileprefix>, --output=<fileprefix>
                        Output data to file output-prefix/<fileprefix>_nnnn
    -s, --sync          Write to the output file after each point
    -h, --help          Show this help
     -t <acquisition-time>, --time=<acquisition-time>
                        Acquisition time [default: 1] """

from os import path
from time import sleep
import sys
import os
import importlib

import py4syn
import py4syn.utils.motor as motorModule
from py4syn.utils.plotter import Plotter
from py4syn.utils.scan import setPostOperationCallback, \
                              setPreOperationCallback, setPrePointCallback,\
                              setPostPointCallback, scan, umv, setOutput,\
                              getScanData,setPlotGraph,\
                              createUserDefinedDataField,\
                              setPartialWrite, setScanComment, getFitValues,\
                              setPreScanCallback, setPostScanCallback


from py4syn.utils.motor import wmr, ummv
from scan_utils.helpers import docopt, listConfigurations, DocoptExit,\
                               processUserField,\
                               loadConfiguration, \
                               readConfiguration, die, loadConstants,\
                               createCounters, createMotor

motorModule.show_info = False


def parseCommandLine(argv):
    p = docopt(__doc__,argv)

    if p['--list-configurations']:
        listConfigurations()
        raise SystemExit(1)

    try:
        # Validate and rename arguments, for easy access
        p['motor'] = p['<motor>']
        p['initial'] = [float(x) for x in p['<initial>']]
        p['final'] = [float(x) for x in p['<final>']]
        p['stepOrCount'] = [float(x) for x in p['<step-or-count>']]
        p['steps'] = [float(x) for x in p['<steps>']]
        p['acquisitionTime'] = [float(x) for x in p['<acquisition-time>']]
        p['time'] = float(p['--time'])
        p['configuration'] = p['--configuration']
        p['optimum'] = p['--optimum']
        p['sync'] = bool(p['--sync'])
        p['output'] = p['--output']
        p['message'] = p['--message']
        p['count'] = int(p['--count'])
        p['sleep'] = float(p['--sleep'])
    except (IndexError, ValueError):
        raise DocoptExit()

    if p['--relative']:
        p['relative'] = True
    elif p['--absolute']:
        p['relative'] = False
    else:
        p['relative'] = None

    return p

def frange(start, end, step, rnd = 5):
    """Generate a range with float values"""
    result = []

    # invert step and change start/end when positions are inverted
    if start > end:
        return(frange(end, start, step*-1))

    if step > 0:
        point = start
        while point <= end:
            result.append(point)
            point =round(point + step,rnd)
    # for negative steps
    else:
        point = end
        while point >= start:
            result.append(point)
            point =round(point + step,rnd)

    return result

def preOperationCallback(sleep_, **kwargs):
    sleep(sleep_)

def plot(plotter, counters, configuration, delta, scan, pos, idx):
    data = getScanData()
    relative = pos[-1]-delta

    for counter in counters:
        c = configuration['counters'][counter]
        if c.get('plot', True):
            plotter.plot(relative, data[counter][-1], c['graph-index'])
        else:
            if c['type'] == 'date' or c['type'] == 'time':
                data[counter].append(processUserField(c))
    try:
        data['delta'].append(relative)
    except KeyError:
        pass


# Override default plot procedure
def configurePlot(counters, configuration, delta, output):
    p = Plotter(output or 'Scan')

    setPlotGraph(False)
    setPostOperationCallback(lambda **kw: plot(p, counters, configuration,
                             delta, **kw))

    return p


# Convert initial, final and other arrays to points and times arrays
def generatePoints(initial, final, stepOrCount, acquisitionTime,
                   configuration):
    stepMode = configuration['misc'].get('step-or-count') == 'step'

    points = []
    times = []

    for i, f, sc, a in zip(initial, final, stepOrCount, acquisitionTime):

        if not stepMode:
            step = (f-i)/sc
            count = int(sc)
        else:
            if f >= i:
                step = sc
            else:
                step = -sc

            count = abs((f-i)/step)

            # Round down from .0 to .9, round up starting at .9 (allows the
            # user to specify slightly imprecise step sizes)
            count = int(count+0.1)

        p = [x*step+i for x in range(count+1)]
        t = [a]*len(p)

        points.extend(p)
        times.extend(t)

    return points, times

def generatePointsSnake(initial, final, steps):
    """Generate points to snake movement"""

    #working for 2 motors
    startrow = initial[0]
    endrow = final[0]
    steprow = steps[0]

    startcol = initial[1]
    endcol = final[1]
    stepcol = steps[1]

    points = [[],[]]

    cols = frange(startcol, endcol, stepcol)

    rows = frange(startrow, endrow, steprow)

    for col in cols:
        for row in rows:
            points[0].append(row)
            points[1].append(col)
        rows.reverse()

    return points, len(cols), len(rows)

class ScanMotors():
    def __init__(self, argv = None, args = None):
        if argv is not None:
            self.args = parseCommandLine(argv)
        elif args is not None:
            self.args = args
        else:
            raise "Arguments necessary!"
        self.motor = self.args['motor']
        self.initial = self.args['initial']
        self.final = self.args['final']
        self.stepOrCount = self.args['stepOrCount']
        self.steps = self.args['steps']
        self.acquisitionTime = self.args['acquisitionTime']
        self.relative = self.args['relative']
        self.sync = self.args['sync']
        self.output = self.args['output']
        self.sleep = self.args['sleep']
        self.comments = self.args['message']
        self.optimum = self.args['optimum']
        self.time = self.args['time']
        self.image = False

    def preScanCallback(self, counters, rows, cols, **kwargs):
        """if a counter is dxp call startcollectimage method"""
        for key, counter in counters.items():
            # k[1] is spectra
            if (key[0]  == 'dxp' or key[0]  == 'dxpfake' or key[0] == 'qe65000') and key[-1]:
                
                counter.startCollectImage(rows, cols)

    def prePointCallback(self, **kwargs):
        pass

    def postPointCallback(self, countersConf, counters, configuration, constants, **kwargs):
        # TODO: procurar um jeito melhor de encontrar o count e o actualCount
        # sem os 2 for's
        for count in countersConf:
            if 'normalize' in configuration['counters'][count]:
                for key, actualCount in counters.items():
                    if key[0] == 'dxp' or key[0] == 'dxpfake' or key[0] == 'qe65000':
                        normalization = configuration['counters'][count]['normalize']
                        if(normalization):
                            counter = {}
                            data = getScanData()
                            for c in data:
                                try:
                                    counter[c] = data[c][-1]
                                except:
                                    pass
                            const = constants
                            try:
                                f = eval(normalization, locals())
                            except ZeroDivisionError:
                                f = 1.0
                            actualCount.setNormValue(f)

    def postScanCallback(self, counters, **kwargs):
        for key, counter in counters.items():
            # k[1] is spectra
            if (key[0]  == 'dxp' or key[0]  == 'dxpfake' or key[0] == 'qe65000') and key[-1]:
                counter.stopCollectImage()


    def runScan(self):

        if len(self.motor) == 1:
            self.motor = self.motor[0]

        try:
            configuration = loadConfiguration()
            counters = readConfiguration('config.' + self.args['configuration'] +
                                         '.yml')
        except OSError as e:
            die(e)

        if self.relative is None:
            self.relative = configuration['misc'].get('default-scan') == 'relative'

#        try:
        countersList = createCounters(counters, configuration, self.output)
        if isinstance(self.motor,list)  and len(self.motor) > 1:
            # 2d mode (snake)
            self.image = True
            for m in self.motor:
                createMotor(m, configuration)
        else:
             createMotor(self.motor, configuration)

#        except (LookupError, ValueError) as e:
#            die(e)

        if self.image:
            points, cols, rows = generatePointsSnake(self.initial, self.final, self.steps)
        else:
            points, times = generatePoints(self.initial, self.final, self.stepOrCount,
                                           self.acquisitionTime, configuration)

            rows = len(points)
            cols = 1
            print(rows)

        # callbacks to collect image
        setPreScanCallback(lambda *l, **kw: self.preScanCallback(countersList, rows,
                           cols, *l, **kw))

        setPostScanCallback(lambda *l, **kw: self.postScanCallback(countersList,
                            *l, **kw))

        constants = loadConstants()[0]
        if self.image:
            oldPosition = []
            for m in self.motor:
                oldPosition.append(wmr(m))
        else:
            oldPosition = wmr(self.motor)

        if self.relative:
            np = []
            for x in points:
                np.append(oldPosition + x)
            points = np
            delta = oldPosition
            createUserDefinedDataField('delta')
        else:
            delta = 0

        setPreOperationCallback(lambda *l, **kw: preOperationCallback(self.sleep, *l,
                                **kw))

        setPrePointCallback(lambda *l, **kw: self.prePointCallback(*l, **kw))

        setPostPointCallback(lambda *l, **kw: self.postPointCallback(counters, countersList, configuration,constants, *l, **kw))

#        plotter = configurePlot(counters, configuration, delta, self.output)

        #if image:
        #    setX(motor[1])
        #    setY(motor[0])
        #else:
        #    setX(motor)

        if self.output:
            setOutput(path.join(configuration['misc']['output-prefix'], self.output))
        if self.comments:
            setScanComment(self.comments)
        if self.sync:
            setPartialWrite(True)

        k = 1
        for i in range(self.args['count']):
            #TODO: remover comentário
            j = 1
#            for counter in counters:
#                if configuration['counters'][counter].get('plot', True):
#                    configuration['counters'][counter]['graph-index'] = k
#                    plotter.createAxis(xlabel="Position", ylabel=counter,
#                                       grid=True, parent=(i and j or None),
#                                       label='%s (%d)' % (counter, i))
#                    j += 1
#                    k += 1

#            try:
            if not self.image:
                scan(self.motor, points, -1, times)
            else:
                # len(points[0]) -> number of steps
                print("Tempo de coleta: ", self.time)
                scan(self.motor[0], points[0], self.motor[1], points[1], len(points[0]),self.time)
#            except Exception as e:
#                die(e)

            print('Scan ended')

            _, peakAt, _, _, _ = getFitValues()
            if peakAt is not None:
                print('Calculated peak position: %g' % peakAt, end='')

                if self.relative:
                    print(' (relative: %+g)' % (peakAt-oldPosition))
                else:
                    print('')

        if self.optimum:
            x = getScanData()[self.motor]
            y = getScanData()[self.optimum]

            m = max(y)
            p = x[y.index(m)]

            print("Max: ", m, " at ", p)
            print("Moving to peak. ")
            umv(self.motor, p)
            print("Motor at ", wmr(self.motor))
        elif self.relative:
            print('Resetting position')
            ummv(**{self.motor: oldPosition})
            print('Done')

#        os.system('python3 /usr/local/scripts/sendMail.py')
#        print('Scan ended. Waiting for graph to close...')
#        plotter.plot_process.join()

if __name__ == '__main__':
    s = ScanMotors(sys.argv[1:])
    s.runScan()
