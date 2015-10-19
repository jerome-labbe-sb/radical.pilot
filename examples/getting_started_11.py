#!/usr/bin/env python

__copyright__ = 'Copyright 2013-2014, http://radical.rutgers.edu'
__license__   = 'MIT'

import os
import sys

os.environ['RADICAL_PILOT_VERBOSE'] = 'REPORT'
os.environ['RADICAL_PILOT_PROFILE'] = 'TRUE'

import radical.pilot as rp
import radical.utils as ru


# ------------------------------------------------------------------------------
#
# READ the RADICAL-Pilot documentation: http://radicalpilot.readthedocs.org/
#
# ------------------------------------------------------------------------------

def myplot():

    import radical.pilot.utils as rpu
    import pprint

    sid = "rp.session.cameo.merzky.016716.0004"

    report = ru.LogReporter(name='radical.pilot')
    report.header('profile analysis')
    report.info('fetch profiles, create data frames, plot\n')

    profiles   = rpu.fetch_profiles(sid=sid, skip_existing=True);    report.progress() 
    profile    = rpu.combine_profiles(profiles); report.progress() 
    frame      = rpu.prof2frame(profile);        report.progress() 
    sf, pf, uf = rpu.split_frame(frame);         report.progress()
    uf         = rpu.add_states(uf);             report.progress()
    uf         = rpu.add_info(uf);               report.progress()
    idf        = rpu.get_info_df(uf);            report.progress()
    sdf        = rpu.get_state_df(idf);          report.progress()

    cols = sorted(list(idf.columns.values))
    pprint.pprint(cols)

  # for col in cols:
  #     print col
  #     print sdf[col][0:3]

    exe_filter = {'in'  : [{'state' : 'Executing'}],
                  'out' : [{'state' : 'AgentStagingOutputPending'}]}
    rpu.add_concurrency(sdf, tgt='cc_exe', spec=exe_filter)
    fin_sdf = sdf[np.isfinite(sdf['cc_exe'])]

    ax_ops = create_figure('Number of concurrently executing CUs over time')
    fin_sdf.plot(x='time', y='cc_exe', ax=ax_ops)

    plot       = rpu.create_plot();              report.progress()
  # rpu.frame_plot ([[sdf, 'frame']], 
  #                 [['time', 'Time (s)'], 
  #                  ['Executing', 'Executing']], 
  #                 title='title', logx=False, logy=False, 
  #                 legend=True, figdir=None)

    report.ok('>>ok\n')

    report.header()


#------------------------------------------------------------------------------
#
if __name__ == '__main__':

    # we use a reporter class for nicer output
    report = ru.LogReporter(name='radical.pilot')
    report.title('Getting Started (RP version %s)' % rp.version)

    myplot()
    sys.exit()

    # use the resource specified as argument, fall back to localhost
    if   len(sys.argv)  > 2: report.exit('Usage:\t%s [resource]\n\n' % sys.argv[0])
    elif len(sys.argv) == 2: resource = sys.argv[1]
    else                   : resource = 'local.localhost'
    if len(sys.argv) > 2:
        report.error('Usage:\t%s [resource]\n\n' % sys.argv[0])
        sys.exit(0)
    elif len(sys.argv) == 2:
        resource = sys.argv[1]
    else:
        resource = 'local.localhost'

    # Create a new session. No need to try/except this: if session creation
    # fails, there is not much we can do anyways...
    session = rp.Session()

    # all other pilot code is now tried/excepted.  If an exception is caught, we
    # can rely on the session object to exist and be valid, and we can thus tear
    # the whole RP stack down via a 'session.close()' call in the 'finally'
    # clause...
    try:

        # read the config used for resource details
        report.info('read config')
        config = ru.read_json('%s/config.json' % os.path.dirname(os.path.abspath(__file__)))
        report.ok('>>ok\n')

        report.header('submit pilots')

        # Add a Pilot Manager. Pilot managers manage one or more ComputePilots.
        pmgr = rp.PilotManager(session=session)

        # Define an [n]-core local pilot that runs for [x] minutes
        # Here we use a dict to initialize the description object
        report.info('create pilot description')
        pd_init = {
                'resource'      : resource,
                'cores'         : 64,  # pilot size
                'runtime'       : 10,  # pilot runtime (min)
                'exit_on_error' : True,
                'project'       : config[resource]['project'],
                'queue'         : config[resource]['queue'],
                'access_schema' : config[resource]['schema']
                }
        pdesc = rp.ComputePilotDescription(pd_init)
        report.ok('>>ok\n')

        # Launch the pilot.
        pilot = pmgr.submit_pilots(pdesc)


        report.header('submit units')

        # Register the ComputePilot in a UnitManager object.
        umgr = rp.UnitManager(session=session)
        umgr.add_pilots(pilot)

        # Create a workload of ComputeUnits. Each compute unit
        # runs '/bin/date'.

        n = 128   # number of units to run
        report.info('create %d unit description(s)\n\t' % n)

        cuds = list()
        for i in range(0, n):

            # create a new CU description, and fill it.
            # Here we don't use dict initialization.
            cud = rp.ComputeUnitDescription()
            cud.executable = '/bin/date'
            cuds.append(cud)
            report.progress()
        report.ok('>>ok\n')

        # Submit the previously created ComputeUnit descriptions to the
        # PilotManager. This will trigger the selected scheduler to start
        # assigning ComputeUnits to the ComputePilots.
        units = umgr.submit_units(cuds)


        # Wait for all compute units to reach a final state (DONE, CANCELED or FAILED).
        report.header('gather results')
        umgr.wait_units()
    
        report.info('\n')
        for unit in units:
            report.plain('  * %s: %s, exit: %3s, out: %s\n' \
                    % (unit.uid, unit.state[:4], 
                        unit.exit_code, unit.stdout.strip()[:35]))


    except Exception as e:
        # Something unexpected happened in the pilot code above
        report.error('caught Exception: %s\n' % e)
        raise

    except (KeyboardInterrupt, SystemExit) as e:
        # the callback called sys.exit(), and we can here catch the
        # corresponding KeyboardInterrupt exception for shutdown.  We also catch
        # SystemExit (which gets raised if the main threads exits for some other
        # reason).
        report.warn('exit requested\n')

    finally:
        # always clean up the session, no matter if we caught an exception or
        # not.  This will kill all remaining pilots.
        report.header('finalize')
        session.close()



#-------------------------------------------------------------------------------
