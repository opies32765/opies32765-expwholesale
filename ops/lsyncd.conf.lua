-- C1 → C2 real-time file replication (C1 is primary; mirror to standby).
-- Mirror of C2's previous config with the host swapped.
settings {
    logfile    = '/var/log/lsyncd/lsyncd.log',
    statusFile = '/var/log/lsyncd/lsyncd.status',
    statusInterval = 20,
    nodaemon   = false,
}
local report_dirs = {
    '/opt/expwholesale/vauto_reports/',
    '/opt/expwholesale/accutrade_reports/',
    '/opt/expwholesale/ipacket_reports/',
    '/opt/expwholesale/static/uploads/',
    '/opt/expwholesale/thumb_cache/',
}
for _, d in ipairs(report_dirs) do
    sync { default.rsync, source = d, target = 'root@84.46.244.0:' .. d,
           rsync = { archive = true, compress = true, verbose = false }, delay = 5 }
end
sync {
    default.rsync,
    source = '/opt/expwholesale/',
    target = 'root@84.46.244.0:/opt/expwholesale/',
    rsync = {
        archive = true, compress = true, verbose = false,
        _extra = {
            '--exclude=venv/', '--exclude=__pycache__/', '--exclude=.git/',
            '--exclude=vauto_reports/', '--exclude=accutrade_reports/',
            '--exclude=ipacket_reports/', '--exclude=static/uploads/',
            '--exclude=thumb_cache/', '--exclude=*.pyc', '--exclude=*.bak.*',
        },
    },
    delay = 5,
}


sync {
    default.rsync,
    source = '/opt/thalist/',
    target = 'root@84.46.244.0:/opt/thalist/',
    rsync = {
        archive = true, compress = true, verbose = false,
        _extra = {
            '--exclude=venv/', '--exclude=__pycache__/',
            '--exclude=*.pyc', '--exclude=*.bak.*',
        },
    },
    delay = 5,
}
