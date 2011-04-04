#!/usr/bin/env python

import os
import sys
import re
import stat
import time
import datetime
import logging
import tempfile
from glob import glob
from string import Template

######################################################################
# EDITABLE CONFIG
######################################################################

DEFAULT_MISSION = 'a31'
DEFAULT_DATA_DIR = '$HOME/data/input/$mission'
DEFAULT_RESULTS_DIR = '$HOME/data/results/$mission/$name'
DEFAULT_RECONSTRUCT_BINARY = './reconstruct'
DEFAULT_SUB = 'sub32'
DEFAULT_NUM_PROCESSORS = 14
# 0 means process all
DEFAULT_NUM_FILES = 0
DEFAULT_REGION = 'all'
DEFAULT_EXTRA = 'std'
DEFAULT_NAME = '${date}_${time}_${subsampleLevel}_${region}_${extra}'

DEFAULT_STEPS = ['weights', '0', '1', '2', 'jpg', 'kml', 'html']

VW_BIN_DIR = '%s/projects/VisionWorkbench/build/x86_64_linux_gcc4.1/bin' % os.environ['HOME']
COLORMAP = '%s/colormap' % VW_BIN_DIR
HILLSHADE = '%s/hillshade' % VW_BIN_DIR
USE_HILLSHADE = True

######################################################################
# GENERIC HELPERS
######################################################################

MAX_EXPAND_STACK_DEPTH = 4

jobQueueG = None

def expand(tmpl, env):
    escaped = re.sub(r'\$\$', '00DOLLAR00', tmpl)
    for i in xrange(0, MAX_EXPAND_STACK_DEPTH):
        oldTmpl = tmpl
        tmpl = Template(tmpl).substitute(env)
        if tmpl == oldTmpl:
            break
    if tmpl != oldTmpl:
        raise ValueError('max expand stack depth exceeded -- variable reference loop?')
    unescaped = re.sub(r'00DOLLAR00', '$', tmpl)
    return unescaped

def dosys(cmd, stopOnErr=False):
    logging.info(cmd)
    ret = os.system(cmd)
    if stopOnErr and ret != 0:
        raise Exception('command exited with non-zero return value %d' % ret)
    return ret

def getTime(f):
    try:
        s = os.stat(f)
    except OSError:
        return 0
    return s[stat.ST_MTIME]

class JobQueue(object):
    def __init__(self, numProcessors=1, deleteTmpFiles=True):
        self.numProcessors = numProcessors
        self.deleteTmpFiles = deleteTmpFiles
        self.jobs = []
        
    def addJob(self, job, step=0):
        logging.debug('addJob %s [step %s]' % (job, step))
        self.jobs.append((step, job))
        
    def clear(self):
        self.jobs = []

    def run(self):
        if not self.jobs:
            # avoid error about max() with no args
            return
        maxStep = max([step for step, job in self.jobs])
        for currentStep in xrange(0, maxStep+1):
            #print '\n*** JobQueue: running step %d of %d ***\n' % (currentStep+1, maxStep+1)
            jobsForStep = [job for jobStep, job in self.jobs
                           if jobStep == currentStep]
            fd, jobsFileName = tempfile.mkstemp('jobQueueStep%d.txt' % currentStep)
            os.close(fd)
            jobsFile = file(jobsFileName, 'w')
            for job in jobsForStep:
                jobsFile.write('%s\n' % job)
            jobsFile.close()
            logging.info('Running JobQueue files from: %s' % jobsFileName)
            dosys('cat %s | xargs -t -P%d -I__cmd__ bash -c __cmd__' % (jobsFileName, self.numProcessors),
                  stopOnErr=True)
        if self.deleteTmpFiles:
            dosys('rm -f %s' % jobsFileName)
        self.clear()

def convertToJpeg(tif, jpg):
    stem = os.path.splitext(tif)[0]
    if 'DEM' in tif:
        # file is a 16-bit TIFF DEM -- colormap to tif then convert to jpg
        cmap = stem + '_CMAP.tif'
        if USE_HILLSHADE:
            hs = stem + '_HILLSHADE.tif'
            jobQueueG.addJob('%s --nodata-value=-32767 %s -o %s' % (HILLSHADE, tif, hs), 0)
            jobQueueG.addJob('rm -f %s' % hs, 2)
            hillshadeArg = '-s %s' % hs
        else:
            hillshadeArg = ''
        jobQueueG.addJob('%s --nodata-value=-32767 %s %s -o %s' % (COLORMAP, hillshadeArg, tif, cmap), 1)
        jobQueueG.addJob('convert %s %s' % (cmap, jpg), 2)
        jobQueueG.addJob('rm -f %s' % cmap, 3)
    else:
        # use GDAL to convert band 1 to jpeg
        # (convert doesn't work well with GRAYA images)
        # (colormap doesn't work well with binary valued GRAYA images)
        jobQueueG.addJob('gdal_translate -b 1 -of JPEG -scale -ot Byte %s %s' % (tif, jpg))

def convertDirectory(opts, suffix):
    resultsDir = expand('$resultsDir', vars(opts))
    jpgDir = '%s/%sJpg' % (resultsDir, suffix)
    jobQueueG.addJob('mkdir -p %s' % jpgDir, 0)
    tifs = glob('%s/%s/*.tif' % (resultsDir, suffix))
    tifs.sort()
    for tif in tifs:
        jpg = '%s/%s.jpg' % (jpgDir, os.path.splitext(os.path.basename(tif))[0])
        if getTime(tif) > getTime(jpg):
            convertToJpeg(tif, jpg)
    jobQueueG.run()

######################################################################
# RECONSTRUCT SCRIPTS
######################################################################

def run_weights(opts, drgs):
    pass # implement me

def runReconstruct(opts, drgs, step):
    resultsDir = expand('$resultsDir', vars(opts))
    dosys('mkdir -p %s' % resultsDir)
    imagesFile = '%s/images.txt' % resultsDir
    out = file(imagesFile, 'w')
    for drg in drgs:
        out.write('1 %s\n' % drg) 
    out.close()

    opts.settings = 'photometry_init_%s_settings.txt' % step
    opts.imagesFile = imagesFile
    for f in drgs:
        opts.inputFile = f
        cmd = '$reconstructBinary -d $dataDir/DEM$undersub -s $dataDir/meta -e $dataDir/meta -r $resultsDir -c $settings -i $inputFile -f $imagesFile'
        jobQueueG.addJob(expand(cmd, vars(opts)))
    jobQueueG.run()

def run_0(opts, drgs):
    runReconstruct(opts, drgs, 0)

def run_1(opts, drgs):
    runReconstruct(opts, drgs, 1)

def run_2(opts, drgs):
    runReconstruct(opts, drgs, 2)

def run_kml(opts, drgs):
    resultsDir = expand('$resultsDir', vars(opts))

    # subsample
    albedoDir = '%s/albedo' % resultsDir
    albedoSmallDir = '%s/albedoSmall' % resultsDir
    dosys('mkdir -p %s' % albedoSmallDir)
    tifs = glob('%s/*.tif' % albedoDir)
    for tif in tifs:
        small = '%s/%s' % (albedoSmallDir, os.path.basename(tif))
        if getTime(tif) > getTime(small):
            jobQueueG.addJob('gdal_translate -outsize 25%% 25%% %s %s'
                             % (tif, small))
    jobQueueG.run()

    # generate kml
    smalls = glob('%s/*.tif' % albedoSmallDir)
    albedoKmlDir = '%s/albedoKml' % resultsDir
    if getTime(smalls[0]) > getTime(albedoKmlDir):
        allSmalls = ' '.join(smalls)
        oldDir = os.getcwd()
        os.chdir(resultsDir)
        logging.info('cwd = %s' % os.getcwd())
        dosys('image2qtree -m kml -o albedoKml %s' % allSmalls)
        os.chdir(oldDir)

    # generate networklink
    netLinkFile = '%s/albedoKml/net.kml' % resultsDir
    netLink = file(netLinkFile, 'w')
    hrefSuffix = expand('', vars(opts))
    text = expand("""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" hint="target=moon">
  <NetworkLink>
    <name>$name/albedoKml</name>
    <Link>
      <href>http://localhost:8080/results/$mission/$name/albedoKml/albedoKml.kml</href>
    </Link>
  </NetworkLink>
</kml>
""",
                  vars(opts))
    netLink.write(text)
    netLink.close()

def run_jpg(opts, drgs):
    convertDirectory(opts, 'shadow')
    convertDirectory(opts, 'reflectance')
    convertDirectory(opts, 'albedo')
    convertDirectory(opts, 'DEM')

ROWS_PER_PAGE = 5

def genHtmlPage(opts, drgs, i, n):
    resultsDir = expand('$resultsDir', vars(opts))
    htmlDir = '%s/html' % resultsDir
    htmlFile = '%s/page%03d.html' % (htmlDir, i)
    startIndex = i*ROWS_PER_PAGE
    rows = drgs[startIndex:(startIndex+ROWS_PER_PAGE)]
    out = file(htmlFile, 'w')
    for i in xrange(n):
        out.write('<a href="page%03d.html">%d</a>\n' % (i, i))
    resultsUrl = expand('/results/$mission/$name', vars(opts))
    for drg in rows:
        drgBase = os.path.splitext(os.path.basename(drg))[0]
        drgBase = re.sub(r'-DRG$', '', drgBase)
        demUrl = '%s/DEMJpg/%s-DEM_out.jpg' % (resultsUrl, drgBase)
        shadowUrl = '%s/shadowJpg/%s-DRG_shadow.jpg' % (resultsUrl, drgBase)
        reflectanceUrl = '%s/reflectanceJpg/%s-DRG_reflectance.jpg' % (resultsUrl, drgBase)
        albedoUrl = '%s/albedoJpg/%s-DRG_albedo.jpg' % (resultsUrl, drgBase)
        out.write('<div>\n')
        out.write('<a href="%s"><img width="128" height="128" src="%s"/></a>\n' % (demUrl, demUrl))
        out.write('<a href="%s"><img width="128" height="128" src="%s"/></a>\n' % (shadowUrl, shadowUrl))
        out.write('<a href="%s"><img width="128" height="128" src="%s"/></a>\n' % (reflectanceUrl, reflectanceUrl))
        out.write('<a href="%s"><img width="128" height="128" src="%s"/></a>\n' % (albedoUrl, albedoUrl))
        out.write('</div>\n')
    out.close()

def run_html(opts, drgs):
    resultsDir = expand('$resultsDir', vars(opts))
    htmlDir = '%s/html' % resultsDir
    dosys('mkdir -p %s' % htmlDir)
    n = len(drgs) // ROWS_PER_PAGE
    for i in xrange(n):
        genHtmlPage(opts, drgs, i, n)

def runStep(opts, drgs, stepName):
    logging.info('')
    logging.info('######################################################################')
    logging.info('# RUNNING STEP %s' % stepName)
    logging.info('######################################################################')
    logging.info('')
    funcName = 'run_%s' % stepName
    if funcName in globals():
        func = globals()[funcName]
    else:
        raise ValueError('invalid step name %s' % stepName)
    func(opts, drgs)

def runMaster(opts):
    global jobQueueG
    jobQueueG = JobQueue(opts.numProcessors)

    if opts.subsampleLevel:
        opts.undersub = '_' + opts.subsampleLevel
    else:
        opts.undersub = ''
    drgPattern = expand('$dataDir/DRG$undersub/*.tif', vars(opts))
    logging.info('Processing DRGs matching pattern: %s' % drgPattern)
    drgs = glob(drgPattern)
    drgs.sort()
    logging.info('Number of matches: %d' % len(drgs))
    if opts.numFiles > 0:
        logging.info('Limiting processing to first %d DRGs' % opts.numFiles)
        drgs = drgs[:opts.numFiles]
    logging.info('First DRG: %s' % drgs[0])
    logging.info('Last DRG:  %s' % drgs[-1])

    for step in opts.steps:
        runStep(opts, drgs, step)

def main():
    import optparse
    parser = optparse.OptionParser('usage: %prog')

    parser.add_option('-m', '--mission',
                      default=DEFAULT_MISSION,
                      help='Mission [%default]')

    parser.add_option('-d', '--dataDir',
                      default=DEFAULT_DATA_DIR,
                      help='Data dir [%default]')

    parser.add_option('-r', '--resultsDir',
                      default=DEFAULT_RESULTS_DIR,
                      help='Results dir [%default]')

    parser.add_option('--reconstructBinary',
                      default=DEFAULT_RECONSTRUCT_BINARY,
                      help='Path to reconstruct binary [%default]')

    parser.add_option('-s', '--subsampleLevel',
                      default=DEFAULT_SUB,
                      help='Subsample level (can be empty) [%default]')

    parser.add_option('-P', '--numProcessors',
                      default=DEFAULT_NUM_PROCESSORS, type='int',
                      help='Number of processors to use [%default]')

    parser.add_option('-n', '--numFiles',
                      default=DEFAULT_NUM_FILES, type='int',
                      help='Number of files to process (0 to process all) [%default]')
    
    parser.add_option('--showConfig',
                      action='store_true', default=False,
                      help='Show config and exit')

    parser.add_option('--region',
                      default=DEFAULT_REGION,
                      help='Region to process [%default]')

    parser.add_option('--extra',
                      default=DEFAULT_EXTRA,
                      help='Extra label that appears at end of name [%default]')

    parser.add_option('--name',
                      default=DEFAULT_NAME,
                      help='Basename of results directory [%default]')

    opts, args = parser.parse_args()

    # stuff extra information into opts
    if args:
        opts.steps = args
    else:
        opts.steps = DEFAULT_STEPS
    opts.HOME = os.environ['HOME']
    opts.date = time.strftime('%Y%m%d')
    opts.time = time.strftime('%H%M%S')

    # set up logging params
    resultsDir = expand('$resultsDir', vars(opts))
    logTimeStr = datetime.datetime.now().isoformat()
    logTimeStr = re.sub(r':', '', logTimeStr)
    logTimeStr = re.sub(r'\.\d+$', '', logTimeStr)
    logTmpl = resultsDir + '/log-%s.txt'
    logFile = logTmpl % logTimeStr
    opts.logFile = logFile

    # debug opts if user requested it
    if opts.showConfig:
        import json
        print json.dumps(vars(opts), indent=4, sort_keys=True)
        sys.exit(0)

    # start logging
    print 'Logging to %s' % logFile
    latestLogFile = logTmpl % 'latest'
    os.system('mkdir -p %s' % resultsDir)
    if os.path.lexists(latestLogFile):
        os.unlink(latestLogFile)
    os.symlink(os.path.basename(logFile), latestLogFile)
    logging.basicConfig(filename=logFile,
                        level=logging.DEBUG,
                        format="%(levelname)-6s %(asctime)-15s %(message)s")

    # do the real work
    runMaster(opts)

if __name__ == '__main__':
    main()