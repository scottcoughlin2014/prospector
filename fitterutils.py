import sys, getopt
import multiprocessing
import numpy as np
import emcee
from scipy.optimize import minimize
try:
    import astropy.io.fits as pyfits
except(ImportError):
    import pyfits
sys.path.append('/home/bjohnson/pfs/apps/clusterfitter')
import observate

lsun, pc = 3.846e33, 3.085677581467192e18 #in cgs
to_cgs = lsun/10**( np.log10(4.0*np.pi)+2*np.log10(pc*10) )


def load_obs(rp):
    if rp['verbose']:
        print('Loading data from {0}'.format(rp['file']))

    fluxconv = np.pi *4. * (rp['dist'] * 1e6 * pc)**2/lsun #erg/s/AA/cm^2 to L_sun/AA
    redshift = rp['vel']/2.998e8

    dat = pyfits.getdata(rp['file'])
    obs ={}
    if rp['file'].split('/')[-2] == 'mmt':
        fluxconv *= 5.0e-20 #approximate counts to erg/s/AA/cm^2
        #dao9
        #mags = np.array([16.9450, 17.4311, 18.1425, 16.9637, 16.1824, 15.4335])
        #mags_unc = np.array([0.0760, 0.0488, 0.0542, 0.0644, 0.0546, 0.0554])
        #AAARGH
        mags = np.array([17.40, 17.35, 17.76, 17.15, 16.94, 16.39])
        mags_unc = np.array([0.076, 0.049, 0.054, 0.064, 0.21, 0.28])
        obs['wavelength'] = np.arange(0, 4540)*1.2+3700.
        obs['spectrum'] = dat[0,:] * fluxconv
        obs['unc'] = np.sqrt(dat[1,:]) * fluxconv
        #Masking.  should move to a function that reads a mask definition file
        obs['mask'] =  ((obs['wavelength'] >= 3750 ) & (obs['wavelength'] <= 7000.))
        obs['mask'] = obs['mask'] & ((obs['wavelength'] <= 6555) | (obs['wavelength'] >= 6590)) #mask NII & Halpha
        obs['mask'] = obs['mask'] & ((obs['wavelength'] <= 5570) | (obs['wavelength'] >= 5590)) #mask OI sky line
    elif rp['file'].split('/')[-2] == 'lris':
        #obj 467
        mags = np.array([18.63547, 19.892399, 19.935846, 19.467628, 19.598925, 21.052458])
        mags_unc = np.array([0.025585078, 0.05667416, 0.06420175, 0.41923323, 0.8458821, 2.6606328])
        sig = np.array([56.38456, 25.312307, 16.934145, 2.7173033, 0.8478325, 0.09438831])
        obs['wavelength'] = dat[0]['wave_opt']
        obs['spectrum'] = dat[0]['spec']
        obs['unc'] = 1./np.sqrt(dat[0]['ivar'])
        obs['mask'] =  ((obs['wavelength'] >= 3550 ) & (obs['wavelength'] <= 5550.))
        
    obs['wavelength'] /= (1.0 + redshift)    
    obs['filters'] = observate.load_filters(['wfc3_uvis_'+b.lower() for b in ["F275W", "F336W", "F475W", "F814W"]] +
                                            ['wfc3_ir_'+b.lower() for b in ["F110W", "F160W"]])
    obs['mags'] = mags - (5.0 * np.log10(rp['dist']) + 25) - np.array([f.ab_to_vega for f in obs['filters']])
    obs['mags_unc'] = mags_unc

    return obs

def parse_args(argv, rp):
    
    shortopt = ''
    try:
        opts, args = getopt.getopt(argv[1:],shortopt,[k+'=' for k in rp.keys()])
    except getopt.GetoptError:
        print 'bsfh_mmt_hyades.py --nthreads <nthreads> --outfile <outfile>'
        sys.exit(2)
    for o, a in opts:
        try:
            rp[o[2:]] = float(a)
        except:
            rp[o[2:]] = a
    if rp['verbose']:
        print('Number of threads  = {0}'.format(rp['nthreads']))
    return rp
        
def initialize_params(chi2, pminimize, model, sps, rp, powell_opt):
    """A kind of tempered minimization"""

    ndim = rp['ndim']
    nthreads = int(rp['nthreads'])
    guess_factor = int(float(rp['guess_factor']))
    nsamplers = int(rp['nsamplers'])
    
    if nthreads > 1:
        #distribute the separate minimizations over processes
        pool = multiprocessing.Pool( nthreads )
        M = pool.map
    else:
        M = map

    # Setup a 'grid' of parameter values uniformly distributed between min and max
    #  More generally, this should sample from the prior for each parameter
    nguess = ndim ** guess_factor #int(2 ** np.round(np.log2(ndim * guess_factor)))
    ginitial = np.zeros([nguess, ndim])
    for p, d in model.theta_desc.iteritems():
        start, stop = d['i0'], d['i0']+d['N']
        hi, lo = d['prior_args']['maxi'], d['prior_args']['mini']
        if d['N'] > 1:
            ginitial[:,start:stop] = np.array([np.random.uniform(h, l, nguess) for h,l in zip(hi,lo)]).T
        else:
            ginitial[:,start] = np.random.uniform(hi, lo, nguess)

    pinitial = ginitial

    # Do quick Powell, then start refining the best of them
    powell_guesses = list( M(pminimize, [ [pinitial[i,:], powell_opt] for i in range(nguess) ]) )

    nrefine = int(nguess/ndim)
    strict_opts = powell_opt.copy()

    while (nrefine > ndim):
        strict_opts['maxfev'] *= ndim
        fsort = np.argsort([pg.fun for pg in powell_guesses])
        powell_guesses = list( M(pminimize, [ [powell_guesses[fsort[i]].x, strict_opts] for i in range(nrefine) ]) )
        nrefine = int(nrefine/ndim)

    #now only focus on as many as you need, and keep them going till you hit convergence
    fsort = np.argsort([pg.fun for pg in powell_guesses])
    for isampler in range( np.min([nsamplers, ndim]) ):
        pg = powell_guesses[fsort[isampler]]
        numit = 0
        while (pg.nfev > strict_opts['maxfev']) and (pg.success is False) and (numit <= 10):
            pg = minimize(chi2, pg.x, method = 'powell',options = strict_opts)
            powell_guesses[fsort[isampler]] = pg
            numit += 1

    if rp['verbose']:
        print('done Powell')

    return [powell_guesses, pinitial, strict_opts]


def run_a_sampler(model, sps, lnprobfn, initial_center, rp):

    ndim = rp['ndim']
    walker_factor = int(rp['walker_factor'])
    nburn = rp['nburn']
    niter = int(rp['niter'])
    nthreads = int(rp['nthreads'])
    initial_disp = rp['initial_disp']
    nwalkers = int(2 ** np.round(np.log2(ndim * walker_factor)))

    initial = np.zeros([nwalkers, ndim])
    for p, d in model.theta_desc.iteritems():
        start, stop = d['i0'], d['i0']+d['N']
        hi, lo = d['prior_args']['maxi'], d['prior_args']['mini']
        initial[:, start:stop] = np.random.normal(1, initial_disp, nwalkers)[:,None] * initial_center[start:stop]

    
    esampler = emcee.EnsembleSampler(nwalkers, ndim, lnprobfn, threads = nthreads, args = [model])

    for iburn in nburn:
        epos, eprob, state = esampler.run_mcmc(initial, iburn)
        # Reinitialize, tossing the worst half of the walkers and resetting
        #   them based on the the other half of the walkers
        #besthalf = eprob > np.median(eprob)
        #worsthalf = eprob < np.median(eprob)
        #relative_scatter = np.abs(epos[besthalf,:].std(axis = 0)/epos[besthalf,:].mean(axis = 0))
        #initial[worsthalf, :] = ( epos[besthalf, :] *
        #                        (1 + np.random.normal(0, 1, (besthalf.sum(), ndim)) * relative_scatter[None,:]) )
        #initial[besthalf, :] = epos[besthalf, :]

        #or just choose the best walker and build a ball around it based on the other walkers
        tmp = np.percentile(epos, [0.25, 0.5, 0.75], axis = 0)
        relative_scatter = np.abs(1.5 * (tmp[2] -tmp[0])/tmp[1])
        best = np.argmax(eprob)
        initial = epos[best,:] * (1 + np.random.normal(0, 1, epos.shape) * relative_scatter[None,:]) 
        esampler.reset()

    epos, eprob, state = esampler.run_mcmc(initial, nburn[-1])
    initial = epos
    esampler.reset()

    epos, eprob, state = esampler.run_mcmc(initial, niter, rstate0 =state)
    return esampler
