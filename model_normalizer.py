"""model_normalizer.py — strip trim/body from LSL model_name to get a base model.

LSL stores model_name as messy free-text:
    "F-150 XLT Pickup Truck Truck"        → "F-150"
    "Wrangler Unlimited Sahara Sport Utility Vehicle"  → "Wrangler Unlimited"
    "GLE-Class GLE350 Sport Utility Vehicle"           → "GLE-Class"
    "Range Rover Sport HSE Sport Utility Vehicle"      → "Range Rover Sport"
    "Sierra 1500 Denali Pickup Truck"                  → "Sierra 1500"

Strategy:
1. Strip trailing body-type phrases (longest match first)
2. Strip trailing trim words token-by-token
3. Apply per-make rules for known multi-word base models
4. If still messy, fall back to the first 1-2 tokens

Validation: dump per-make normalized counts to confirm we're collapsing
similar models together, not over-merging distinct ones.
"""
from __future__ import annotations
import re

# Body-type phrases (longest first — match greedily)
_BODY_PHRASES = [
    'Sport Utility Vehicle', 'Pickup Truck Truck', 'Pickup Truck',
    'Sport Utility', 'Crew Cab', 'Quad Cab', 'Extended Cab',
    'Regular Cab', 'Mega Cab', 'Cabriolet', 'Convertible',
    'Roadster', 'Hatchback', 'Targa', 'Spider', 'Spyder',
    'Coupe', 'Sedan', 'Wagon', 'Minivan', 'Van', 'Truck',
    'Hybrid', 'Plug-in Hybrid', 'PHEV', 'EV', 'Electric',
    'AWD', 'RWD', 'FWD', '4WD', '4MATIC', '4Matic', 'XDrive',
    'xDrive', 'Quattro', 'quattro', '4 Door', '4 Dr.', '4 Dr',
    '2 Door', '2 Dr.', '2 Dr', '4D', '2D',
]

# Trim word tokens (case-insensitive, dropped from end one at a time)
_TRIM_TOKENS = {
    'premium','plus','prestige','luxury','sport','base',
    'limited','overland','rubicon','sahara','willys',
    'autobiography','hse','svr','svo','r-dynamic','firstedition',
    'platinum','denali','signature','high','country','elevation',
    'technology','package','navigation','tech',
    'f','sport',  # Lexus F Sport
    'amg','m','rs','s','rs6','rs5','rs4','rs3',
    'lt','ls','ltz','sle','slt','rst','wt','custom',
    'le','xle','se','sel','sl','sv','xse','trd','trd-pro',
    'edition','launch','collection','centennial','heritage',
    'first',
    # purely punctuation/numerics-only that snuck in:
    '4mat','4matic',
}

# Per-make multi-word base models that must be preserved as a whole
# even if their tail words look like trim
_PER_MAKE_BASES = {
    'JEEP': [
        'Grand Cherokee L', 'Grand Cherokee', 'Grand Wagoneer',
        'Wagoneer', 'Wrangler Unlimited', 'Wrangler 4xe',
        'Wrangler', 'Cherokee', 'Compass', 'Renegade',
        'Gladiator', 'Patriot', 'Liberty', 'Commander',
    ],
    'LAND ROVER': [
        'Range Rover Sport', 'Range Rover Velar', 'Range Rover Evoque',
        'Range Rover Defender', 'Range Rover',
        'Discovery Sport', 'Discovery',
        'LR3', 'LR4', 'LR2',
    ],
    'CHEVROLET': [
        'Silverado 1500', 'Silverado 2500', 'Silverado 2500HD',
        'Silverado 3500', 'Silverado 3500HD',
        'Corvette Stingray', 'Corvette Z06', 'Corvette ZR1',
        'Corvette Z51', 'Corvette',
        'Suburban', 'Tahoe', 'Equinox', 'Traverse', 'Trailblazer',
        'Camaro', 'Malibu', 'Impala', 'Sonic', 'Spark', 'Cruze',
        'Trax', 'Blazer', 'Bolt EV', 'Bolt EUV', 'Colorado',
    ],
    'FORD': [
        'F-150 Lightning', 'F-150 Raptor', 'F-150',
        'F-250 SD', 'F-250', 'F-350 SD', 'F-350', 'F-450',
        'Mustang Mach-E', 'Mustang',
        'Bronco Sport', 'Bronco',
        'Explorer', 'Edge', 'Escape', 'Expedition', 'Excursion',
        'Maverick', 'Ranger', 'Transit', 'Fusion', 'Focus',
        'Fiesta', 'Taurus', 'Flex', 'EcoSport',
    ],
    'GMC': [
        'Sierra 1500', 'Sierra 2500', 'Sierra 2500HD',
        'Sierra 3500', 'Sierra 3500HD',
        'Yukon XL', 'Yukon',
        'Acadia', 'Terrain', 'Canyon', 'Hummer EV',
    ],
    'TOYOTA': [
        'Land Cruiser', 'Sequoia', 'Highlander Hybrid', 'Highlander',
        '4Runner', 'Corolla Cross', 'Corolla', 'Camry', 'Avalon',
        'Tundra', 'Tacoma', 'Sienna', 'RAV4 Prime', 'RAV4',
        'Prius Prime', 'Prius', 'Mirai', 'GR Supra', 'GR86',
        'GR Corolla', 'Venza', 'C-HR', 'Crown', 'bZ4X',
    ],
    'HONDA': [
        'CR-V Hybrid', 'CR-V', 'HR-V', 'BR-V',
        'Civic Type R', 'Civic Si', 'Civic',
        'Accord Hybrid', 'Accord',
        'Pilot', 'Passport', 'Ridgeline', 'Odyssey',
        'Insight', 'Fit', 'Clarity', 'Element',
    ],
    'BMW': [
        '1-Series','2-Series','3-Series','4-Series','5-Series',
        '6-Series','7-Series','8-Series',
        'X1','X2','X3','X4','X5','X6','X7','XM',
        'M2','M3','M4','M5','M6','M8',
        'Z4','i3','i4','i5','i7','i8','iX',
    ],
    'MERCEDES-BENZ': [
        'A-Class','C-Class','E-Class','S-Class','G-Class','GLA-Class',
        'GLB-Class','GLC-Class','GLE-Class','GLS-Class','GLK-Class',
        'CLA-Class','CLS-Class','CL-Class','SL-Class','SLK-Class',
        'SLC-Class','AMG GT','EQS','EQE','EQB','EQC','EQA',
        'Maybach S-Class','Sprinter','Metris',
    ],
    'PORSCHE': [
        '911','718 Boxster','718 Cayman','718 Spyder','718 GT4',
        'Boxster','Cayman','Cayenne Coupe','Cayenne','Macan',
        'Panamera','Taycan',
    ],
    'AUDI': [
        'A3','A4','A5','A6','A7','A8','Q3','Q4 e-tron','Q4',
        'Q5','Q7','Q8','e-tron','e-tron GT',
        'S3','S4','S5','S6','S7','S8','SQ5','SQ7','SQ8',
        'RS3','RS4','RS5','RS6','RS7','RS Q8','RS e-tron GT',
        'R8','TT','TTS','TT RS',
    ],
    'CADILLAC': [
        'Escalade ESV','Escalade IQ','Escalade',
        'XT4','XT5','XT6','CT4','CT5','CT6',
        'CTS','ATS','XTS','ELR','SRX','STS','DTS','Lyriq','Celestiq',
    ],
    'DODGE': [
        'Charger','Challenger','Durango','Journey','Hornet','Caliber',
        'Avenger','Caravan','Grand Caravan','Magnum','Viper','Dart',
    ],
    'RAM': [
        '1500','2500','3500','4500','5500','ProMaster',
        '1500 Classic',
    ],
    'NISSAN': [
        'Altima','Maxima','Sentra','Versa','Leaf','GT-R','Z',
        '370Z','350Z','Murano','Pathfinder','Armada','Rogue',
        'Kicks','Frontier','Titan','Titan XD','Juke','Cube',
        'Quest','Xterra','Ariya',
    ],
    'LEXUS': [
        'IS','ES','GS','LS','RC','LC','UX','NX','RX','GX','LX',
        'CT','HS','SC','RC F','LFA','RZ',
    ],
    'TESLA': ['Model S','Model 3','Model X','Model Y','Cybertruck','Roadster'],
    'VOLKSWAGEN': [
        'Atlas Cross Sport','Atlas','Tiguan','Taos',
        'Golf GTI','Golf R','Golf','Jetta','Passat',
        'Arteon','ID.4','ID.7','ID.Buzz','Beetle','CC','Eos','Touareg',
    ],
    'HYUNDAI': [
        'Tucson Hybrid','Tucson','Santa Fe Hybrid','Santa Fe','Santa Cruz',
        'Palisade','Kona Electric','Kona','Sonata Hybrid','Sonata',
        'Elantra','Accent','Ioniq 5','Ioniq 6','Ioniq','Genesis',
        'Veloster','Venue',
    ],
    'KIA': [
        'Telluride','Sportage Hybrid','Sportage','Sorento Hybrid','Sorento',
        'Carnival','Stinger','K5','K900','Optima','Forte','Soul EV','Soul',
        'Niro EV','Niro','EV6','EV9','Rio','Sedona','Cadenza','Seltos',
    ],
    'MAZDA': [
        'CX-3','CX-30','CX-5','CX-50','CX-7','CX-9','CX-50 Hybrid',
        'MX-5 Miata','MX-30','MX-30 EV','Mazda3','Mazda6','Mazda5',
        'CX-90','CX-9','RX-8','RX-7',
    ],
}

# Normalize make name for lookup
def _make_norm(s):
    if not s: return ''
    return s.upper().strip()


# Pre-build a sorted list (longest first) per make so we match greedily
_PER_MAKE_BASES_SORTED = {
    mk: sorted(bases, key=lambda b: -len(b))
    for mk, bases in _PER_MAKE_BASES.items()
}


def _strip_body(s: str) -> str:
    """Strip trailing body-type phrases (longest match first)."""
    s = s.strip()
    while True:
        before = s
        # Try each body phrase as a trailing word
        for phrase in _BODY_PHRASES:
            if s.lower().endswith(' ' + phrase.lower()):
                s = s[: -(len(phrase) + 1)].rstrip()
                break
            if s.lower() == phrase.lower():
                s = ''
                break
        if s == before:
            break
    return s


def _strip_trim_tokens(s: str) -> str:
    """Pop trim tokens from the end one at a time."""
    tokens = s.split()
    while len(tokens) > 1 and tokens[-1].lower().strip('.') in _TRIM_TOKENS:
        tokens.pop()
    return ' '.join(tokens)


def normalize_model(make: str | None, model_name: str | None) -> str | None:
    """Return base-model string, or None if input is empty/garbage."""
    if not model_name:
        return None
    s = model_name.strip()
    if not s:
        return None

    # Drop a leading make prefix if present (some rows have it)
    if make and s.upper().startswith(make.upper() + ' '):
        s = s[len(make) + 1:].strip()
    if not s:
        return None

    mk = _make_norm(make)

    # 1) Per-make hard match: pick the longest base that prefixes s
    for base in _PER_MAKE_BASES_SORTED.get(mk, []):
        if s.lower() == base.lower() or s.lower().startswith(base.lower() + ' '):
            return base

    # 2) Strip body-type phrases from end
    s = _strip_body(s)

    # 3) Try per-make match again (strips may have exposed it)
    for base in _PER_MAKE_BASES_SORTED.get(mk, []):
        if s.lower() == base.lower() or s.lower().startswith(base.lower() + ' '):
            return base

    # 4) Strip trim tokens
    s = _strip_trim_tokens(s)

    # 5) Try per-make match a final time
    for base in _PER_MAKE_BASES_SORTED.get(mk, []):
        if s.lower() == base.lower() or s.lower().startswith(base.lower() + ' '):
            return base

    # 6) Fallback: if the cleaned-up string is still 3+ tokens, keep the
    # first 2; else return what we have.
    tokens = s.split()
    if len(tokens) > 2:
        return ' '.join(tokens[:2])
    return s if s else None


# ── Self-test / validation ────────────────────────────────────────────────

if __name__ == '__main__':
    samples = [
        ('FORD',           'F-150 XLT Pickup Truck',                 'F-150'),
        ('FORD',           'F-150 XLT Pickup Truck Truck',           'F-150'),
        ('FORD',           'F-150 Raptor Pickup Truck',              'F-150 Raptor'),
        ('FORD',           'Mustang EcoBoost Convertible',           'Mustang'),
        ('JEEP',           'Wrangler Unlimited Sahara Sport Utility Vehicle', 'Wrangler Unlimited'),
        ('JEEP',           'Grand Cherokee Limited Sport Utility Vehicle',    'Grand Cherokee'),
        ('JEEP',           'Wrangler',                                'Wrangler'),
        ('LAND ROVER',     'Range Rover Sport HSE Sport Utility Vehicle',     'Range Rover Sport'),
        ('LAND ROVER',     'Range Rover',                             'Range Rover'),
        ('LAND ROVER',     'Range Rover Velar R-Dynamic SE Sport Utility Vehicle', 'Range Rover Velar'),
        ('CHEVROLET',      'Silverado 1500 LT Pickup Truck',          'Silverado 1500'),
        ('CHEVROLET',      'Corvette Stingray',                       'Corvette Stingray'),
        ('CHEVROLET',      'Corvette',                                'Corvette'),
        ('MERCEDES-BENZ',  'GLE-Class GLE350 Sport Utility Vehicle',  'GLE-Class'),
        ('MERCEDES-BENZ',  'S-Class S580 4MATIC Sedan',               'S-Class'),
        ('MERCEDES-BENZ',  'G-Class G63 AMG Sport Utility Vehicle',   'G-Class'),
        ('PORSCHE',        '911',                                     '911'),
        ('PORSCHE',        '718 Boxster',                             '718 Boxster'),
        ('GMC',            'Yukon Denali Sport Utility Vehicle',      'Yukon'),
        ('GMC',            'Yukon XL Denali Sport Utility Vehicle',   'Yukon XL'),
        ('GMC',            'Sierra 1500 Denali Pickup Truck',         'Sierra 1500'),
        ('GMC',            'Sierra 1500 Denali Pickup Truck Truck',   'Sierra 1500'),
        ('AUDI',           'Q5 Premium Plus Sport Utility Vehicle',   'Q5'),
        ('AUDI',           'A4',                                      'A4'),
        ('AUDI',           'RS6',                                     'RS6'),
        ('CADILLAC',       'Escalade ESV Sport Sport Utility Vehicle','Escalade ESV'),
        ('CADILLAC',       'Escalade Premium Luxury Sport Utility Vehicle', 'Escalade'),
        ('LEXUS',          'RX 350 Sport Utility Vehicle',            'RX'),
        ('TOYOTA',         'Tundra SR5 Pickup Truck',                 'Tundra'),
        ('BMW',            'X5 xDrive40i Sport Utility Vehicle',      'X5'),
        ('BMW',            'M3 Sedan',                                'M3'),
        ('BMW',            '7-Series 740i Sedan',                     '7-Series'),
    ]

    print(f"{'PASS':<5} {'make':<14} {'input':<48} {'expected':<22} got")
    print('-' * 120)
    n_pass = n_fail = 0
    for make, inp, expected in samples:
        got = normalize_model(make, inp)
        ok = got == expected
        n_pass += ok
        n_fail += not ok
        flag = 'PASS' if ok else 'FAIL'
        print(f"{flag:<5} {make:<14} {inp[:47]:<48} {expected:<22} {got}")
    print(f"\n{n_pass}/{n_pass+n_fail} passed")
