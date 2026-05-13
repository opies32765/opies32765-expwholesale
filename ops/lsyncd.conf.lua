-- C2 → C1 real-time file replication for EW.
-- No --delete: file removal stays local; periodic prune handles cleanup.
-- Extended 2026-05-13 to include source code + state/ for true hot standby.

settings {
    logfile    = '/var/log/lsyncd/lsyncd.log',
    statusFile = '/var/log/lsyncd/lsyncd.status',
    statusInterval = 20,
    nodaemon   = false,
}

-- Report directories — large, write-heavy, individual sync
local report_dirs = {
    '/opt/expwholesale/vauto_reports/',
    '/opt/expwholesale/accutrade_reports/',
    '/opt/expwholesale/ipacket_reports/',
    '/opt/expwholesale/static/uploads/',
    '/opt/expwholesale/thumb_cache/',
}
for _, d in ipairs(report_dirs) do
    sync {
        default.rsync,
        source = d,
        target = 'root@62.146.226.100:' .. d,
        rsync = { archive = true, compress = true, verbose = false },
        delay = 5,
    }
end

-- Top-level EW dir: Python source + state/ + scripts/ etc.
-- Excludes the report dirs (synced above) + venv/git/cache for efficiency.
sync {
    default.rsync,
    source = '/opt/expwholesale/',
    target = 'root@62.146.226.100:/opt/expwholesale/',
    rsync = {
        archive = true,
        compress = true,
        verbose = false,
        _extra = {
            '--exclude=venv/',
            '--exclude=__pycache__/',
            '--exclude=.git/',
            '--exclude=vauto_reports/',
            '--exclude=accutrade_reports/',
            '--exclude=ipacket_reports/',
            '--exclude=static/uploads/',
            '--exclude=thumb_cache/',
            '--exclude=*.pyc',
            '--exclude=*.bak.*',
        },
    },
    delay = 5,
}
