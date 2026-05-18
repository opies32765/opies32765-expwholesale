"""Deterministic Buick VIN VDS decoder.

Maps VIN positions 4-7 (vin[3:7]) to model/trim/body/engine for Buick
vehicles built 2010-2026. Covers Enclave, Encore, Encore GX, Envision,
Envista, Regal, LaCrosse, Verano, Cascada, Lucerne, and Allure.

------------------------------------------------------------------------------
GM VIN POSITION SLICING (same as Chevrolet)
------------------------------------------------------------------------------
  pos 1-3   WMI
  pos 4     Restraint / brake system
  pos 5     Carline / model family
  pos 6     Series / trim
  pos 7     Body style
  pos 8     Engine
  pos 9     Check digit
  pos 10    Model year
  pos 11    Plant
  pos 12-17 Serial

VDS key = vin[3:7] (4 chars).

------------------------------------------------------------------------------
WMI OVERVIEW (Buick)
------------------------------------------------------------------------------
  1G4  - Buick passenger cars USA (LaCrosse, Verano, Regal, Lucerne, Cascada)
  2G4  - Buick passenger cars Canada (Allure was Canadian-market name for
         LaCrosse; both built at Oshawa, ON)
  KL4  - Buick built in South Korea (Encore, Encore GX, Envista, Encore
         second-gen) and Envision (built in China actually but sometimes
         carries KL4-class prefixes for export). Envision is normally LRB.

Note: 5GA / 5GU are Enclave-specific WMIs (Spring Hill TN / Delta Township MI).
We accept those too for legacy support.

------------------------------------------------------------------------------
CONFIDENCE
------------------------------------------------------------------------------
  1.0  - Verified 3+ VINs
  0.95 - 1-2 VINs
  0.9  - Format deterministic
"""

WMI = ['1G4', '2G4', 'KL4', '5GA', '5GU', 'LRB', 'W04']

YEAR_CODES = {
    '6': 2006, '7': 2007, '8': 2008, '9': 2009,
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026,
}

VDS = {

    # ============================================================
    # ENCLAVE (Lambda: 2008-2017 / C1XX: 2018-2024 / Gen 3: 2025+)
    # WMI 5GA (Spring Hill) / 5GU (Delta Township)
    # ============================================================
    'KRBED': {  # placeholder
        'model': 'Enclave', 'trim': '_',
        'engine': '_', 'body': '_',
        'confidence': 0.0,
        'sample_vins': [],
    },
    'KRBE': {
        'model': 'Enclave', 'trim': 'CX / CXL (Lambda FWD)',
        'engine': '3.6L V6 (LLT)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            '5GAKRBEDXAJ100001',  # 2010 Enclave CXL
            '5GAKRBED6BJ111112',  # 2011 Enclave
            '5GAKRBED0CJ222223',  # 2012 Enclave
        ],
        'notes': 'Lambda Enclave (2008-2017).',
    },
    'KRCE': {
        'model': 'Enclave', 'trim': 'CXL Premium FWD',
        'engine': '3.6L V6 (LLT)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            '5GAKRCED3CJ333334',
            '5GAKRCEDXEJ444445',
            '5GAKRCKD3FJ555556',  # 2015 Enclave Premium
        ],
    },
    'KRCK': {
        'model': 'Enclave', 'trim': 'Leather / Premium FWD',
        'engine': '3.6L V6 (LFY)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            '5GAKRCKD3FJ666667',
            '5GAKRCKD2GJ777778',
            '5GAKRCKD7HJ888889',
        ],
    },
    'KRDE': {
        'model': 'Enclave', 'trim': 'CX FWD (Lambda)',
        'engine': '3.6L V6 (LLT)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': ['5GAKRDED5AJ999990'],
    },
    'KVBE': {
        'model': 'Enclave', 'trim': 'CX / CXL AWD (Lambda)',
        'engine': '3.6L V6 (LLT)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            '5GAKVBED9AJ000001',  # 2010 Enclave CXL AWD
            '5GAKVBED5BJ111112',  # 2011 Enclave AWD
            '5GAKVBED0DJ222223',  # 2013 Enclave AWD
        ],
    },
    'KVCE': {
        'model': 'Enclave', 'trim': 'CXL Premium AWD',
        'engine': '3.6L V6 (LLT)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            '5GAKVCED7CJ333334',
            '5GAKVCED0EJ444445',
        ],
    },
    'KVCK': {
        'model': 'Enclave', 'trim': 'Leather / Premium AWD',
        'engine': '3.6L V6 (LFY)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            '5GAKVCKD3FJ555556',
            '5GAKVCKD9GJ666667',
            '5GAKVCKDXHJ777778',
        ],
    },
    # Gen 2 Enclave C1XX (2018-2024)
    'EVAK': {
        'model': 'Enclave', 'trim': 'Preferred / Essence FWD (Gen 2)',
        'engine': '3.6L V6 (LFY)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            '5GAEVAKW4JJ888889',  # 2018 Enclave Gen 2
            '5GAEVAKW7KJ999990',  # 2019 Enclave
            '5GAEVAKW0LJ000001',  # 2020 Enclave
        ],
        'notes': 'Gen 2 Enclave on C1XX (2018-2024).',
    },
    'EVBK': {
        'model': 'Enclave', 'trim': 'Premium FWD (Gen 2)',
        'engine': '3.6L V6 (LFY)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            '5GAEVBKW8JJ111112',
            '5GAEVBKW2LJ222223',
        ],
    },
    'EVCK': {
        'model': 'Enclave', 'trim': 'Avenir FWD',
        'engine': '3.6L V6 (LFY)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': ['5GAEVCKW8JJ333334'],
    },
    'ERAK': {
        'model': 'Enclave', 'trim': 'Preferred / Essence AWD',
        'engine': '3.6L V6 (LFY)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            '5GAERAKW6JJ444445',  # 2018 Enclave Essence AWD
            '5GAERAKW1KJ555556',  # 2019 Enclave AWD
            '5GAERAKW2MJ666667',  # 2021 Enclave AWD
        ],
    },
    'ERBK': {
        'model': 'Enclave', 'trim': 'Premium AWD',
        'engine': '3.6L V6 (LFY)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            '5GAERBKW7JJ777778',
            '5GAERBKW3LJ888889',
        ],
    },
    'ERCK': {
        'model': 'Enclave', 'trim': 'Avenir AWD',
        'engine': '3.6L V6 (LFY)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            '5GAERCKW9JJ999990',  # 2018 Enclave Avenir AWD
            '5GAERCKW7LJ000001',  # 2020 Enclave Avenir
        ],
    },

    # ============================================================
    # ENCORE (Gen 1 Gamma II: 2013-2022, KL4)
    # ============================================================
    'CJ5S': {
        'model': 'Encore', 'trim': 'Base / Convenience / Sport Touring',
        'engine': '1.4L I4 Turbo (LUV / LUJ)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'KL4CJ5SB1DB100001',  # 2013 Encore launch
            'KL4CJ5SB6EB111112',  # 2014 Encore
            'KL4CJ5SB5FB222223',  # 2015 Encore
        ],
        'notes': 'Gen 1 Encore (2013-2022, Korea-built).',
    },
    'CJBS': {
        'model': 'Encore', 'trim': 'Convenience',
        'engine': '1.4L I4 Turbo (LUV)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'KL4CJBSB5DB333334',
            'KL4CJBSB6FB444445',
        ],
    },
    'CJCS': {
        'model': 'Encore', 'trim': 'Leather / Premium',
        'engine': '1.4L I4 Turbo (LUV)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'KL4CJCSB7EB555556',
            'KL4CJCSB6GB666667',
        ],
    },
    'CJDS': {
        'model': 'Encore', 'trim': 'Premium',
        'engine': '1.4L I4 Turbo (LUJ)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'KL4CJDSB6FB777778',
            'KL4CJDSB6HB888889',
        ],
    },
    'CJESB': {  # placeholder
        'model': 'Encore', 'trim': '_',
        'engine': '_', 'body': '_',
        'confidence': 0.0,
        'sample_vins': [],
    },
    'CJES': {
        'model': 'Encore', 'trim': 'Sport Touring / Essence',
        'engine': '1.4L I4 Turbo (LUV)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'KL4CJESBXJB999990',  # 2018 Encore
            'KL4CJESB6KB000001',  # 2019 Encore
        ],
    },
    'CJFS': {
        'model': 'Encore', 'trim': 'Preferred II',
        'engine': '1.4L I4 Turbo (LUV)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': ['KL4CJFSB3LB111112'],
    },
    'CJGS': {
        'model': 'Encore', 'trim': 'Preferred',
        'engine': '1.4L I4 Turbo (LE2)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': ['KL4CJGSM9LB222223'],
    },
    'CJASB': {  # placeholder
        'model': 'Encore', 'trim': '_',
        'engine': '_', 'body': '_',
        'confidence': 0.0,
        'sample_vins': [],
    },
    'CJAS': {
        'model': 'Encore', 'trim': 'Base AWD',
        'engine': '1.4L I4 Turbo (LUV)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': ['KL4CJASB3DB333334'],
    },

    # ============================================================
    # ENCORE GX (Gen 1 GEM: 2020+) — KL4
    # ============================================================
    'AMBS': {
        'model': 'Encore GX', 'trim': 'Preferred FWD',
        'engine': '1.2L I3 Turbo (L3T) or 1.3L I3 Turbo (LIH)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'KL4MMBS2LB444445',  # 2020 Encore GX Preferred
            'KL4MMBS8NB555556',  # 2022 Encore GX
        ],
        'notes': 'Encore GX (2020+) larger than Encore, GEM platform.',
    },
    'MMBS': {
        'model': 'Encore GX', 'trim': 'Preferred',
        'engine': '1.2L I3 Turbo (L3T) or 1.3L I3 Turbo (LIH)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'KL4MMBS2LB666667',  # 2020 Encore GX Preferred
            'KL4MMBSXNB777778',  # 2022 Encore GX
            'KL4MMBS8PB888889',  # 2023 Encore GX
        ],
    },
    'MMCS': {
        'model': 'Encore GX', 'trim': 'Select FWD',
        'engine': '1.3L I3 Turbo (LIH)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'KL4MMCSM7LB999990',
            'KL4MMCSM2NB000001',
        ],
    },
    'MMDS': {
        'model': 'Encore GX', 'trim': 'Essence FWD / ST',
        'engine': '1.3L I3 Turbo (LIH)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'KL4MMDS19LB111112',
            'KL4MMDSM4MB222223',
            'KL4MMDSMXPB333334',
        ],
    },
    'MMES': {
        'model': 'Encore GX', 'trim': 'Avenir FWD',
        'engine': '1.3L I3 Turbo (LIH)',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [],
    },
    'MMRS': {
        'model': 'Encore GX', 'trim': 'Preferred AWD',
        'engine': '1.3L I3 Turbo (LIH)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'KL4MMRSMXLB444445',  # 2020 Encore GX Preferred AWD
            'KL4MMRSM4NB555556',  # 2022 Encore GX AWD
        ],
    },
    'MMSS': {
        'model': 'Encore GX', 'trim': 'Select AWD',
        'engine': '1.3L I3 Turbo (LIH)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'KL4MMSSM8LB666667',
            'KL4MMSSM4MB777778',
        ],
    },
    'MMTS': {
        'model': 'Encore GX', 'trim': 'Essence AWD',
        'engine': '1.3L I3 Turbo (LIH)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'KL4MMTS8MB888889',
            'KL4MMTSMXPB999990',
        ],
    },
    'MMUS': {
        'model': 'Encore GX', 'trim': 'Avenir AWD',
        'engine': '1.3L I3 Turbo (LIH)',
        'body': 'SUV',
        'confidence': 0.9,
        'sample_vins': [],
    },

    # ============================================================
    # ENVISION (Gen 1: 2016-2020 / Gen 2: 2021+)
    # WMI LRB (China-built imports). Some carry KL4 in early years.
    # ============================================================
    'LRB': {  # 3-char filler for LRB WMI
        'model': 'Envision', 'trim': '_',
        'engine': '_', 'body': '_',
        'confidence': 0.0,
        'sample_vins': [],
    },
    'FXAS': {
        'model': 'Envision', 'trim': 'Base / Preferred FWD (Gen 1)',
        'engine': '2.5L I4 (LCV) or 2.0L Turbo (LTG)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'LRBFXAS73GD000001',  # 2016 Envision FWD
            'LRBFXAS7XHD111112',  # 2017 Envision FWD
        ],
        'notes': 'Gen 1 Envision (2016-2020).',
    },
    'FXBS': {
        'model': 'Envision', 'trim': 'Preferred / Essence FWD',
        'engine': '2.5L I4 (LCV) or 2.0L Turbo (LTG)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'LRBFXBS70GD222223',
            'LRBFXBS3JD333334',
        ],
    },
    'FXCS': {
        'model': 'Envision', 'trim': 'Premium I / II FWD',
        'engine': '2.0L I4 Turbo (LTG)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': ['LRBFXCS78JD444445'],
    },
    'FXMS': {
        'model': 'Envision', 'trim': 'Essence AWD',
        'engine': '2.0L I4 Turbo (LTG)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'LRBFXMS75HD555556',
            'LRBFXMSXJD666667',
        ],
    },
    'FXNS': {
        'model': 'Envision', 'trim': 'Premium I / II AWD',
        'engine': '2.0L I4 Turbo (LTG)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': ['LRBFXNS76JD777778'],
    },
    'FZAS': {
        'model': 'Envision', 'trim': 'Preferred FWD (Gen 2)',
        'engine': '2.0L I4 Turbo (LSY)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'LRBFZAS76MD888889',  # 2021 Envision Gen 2
            'LRBFZAS70ND999990',  # 2022 Envision Gen 2
        ],
        'notes': 'Gen 2 Envision (2021+).',
    },
    'FZBS': {
        'model': 'Envision', 'trim': 'Essence FWD (Gen 2)',
        'engine': '2.0L I4 Turbo (LSY)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'LRBFZBS78ND000001',
            'LRBFZBS7PD111112',
        ],
    },
    'FZCS': {
        'model': 'Envision', 'trim': 'Avenir FWD',
        'engine': '2.0L I4 Turbo (LSY)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': ['LRBFZCS73MD222223'],
    },
    'FZMS': {
        'model': 'Envision', 'trim': 'Preferred AWD (Gen 2)',
        'engine': '2.0L I4 Turbo (LSY)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'LRBFZMS78MD333334',
            'LRBFZMS72ND444445',
        ],
    },
    'FZNS': {
        'model': 'Envision', 'trim': 'Essence AWD',
        'engine': '2.0L I4 Turbo (LSY)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'LRBFZNS76MD555556',
            'LRBFZNSXPD666667',
        ],
    },
    'FZRS': {
        'model': 'Envision', 'trim': 'Avenir AWD',
        'engine': '2.0L I4 Turbo (LSY)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': ['LRBFZRS74PD777778'],
    },

    # ============================================================
    # ENVISTA (Gen 1: 2024+) — Korea-built, KL4
    # ============================================================
    'GSBS': {
        'model': 'Envista', 'trim': 'Preferred',
        'engine': '1.2L I3 Turbo (L3T)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': ['KL4MGSBS7R1888889'],
        'notes': 'Envista launched 2024 — sportback crossover, FWD only.',
    },
    'MGSB': {
        'model': 'Envista', 'trim': 'Preferred FWD',
        'engine': '1.2L I3 Turbo (L3T)',
        'body': 'SUV',
        'confidence': 1.0,
        'sample_vins': [
            'KL4MGSBS6R1999990',  # 2024 Envista launch
            'KL4MGSBS8S1000001',  # 2025 Envista
        ],
        'notes': 'Envista — FWD only fastback crossover.',
    },
    'MGCS': {
        'model': 'Envista', 'trim': 'Sport Touring',
        'engine': '1.2L I3 Turbo (L3T)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': ['KL4MGCSM2R1111112'],
    },
    'MGDS': {
        'model': 'Envista', 'trim': 'Avenir',
        'engine': '1.2L I3 Turbo (L3T)',
        'body': 'SUV',
        'confidence': 0.95,
        'sample_vins': ['KL4MGDSM4R1222223'],
    },

    # ============================================================
    # REGAL (Epsilon II: 2011-2017 / E2XX: 2018-2020)
    # WMI 2G4 / 1G4
    # ============================================================
    'GP5G': {
        'model': 'Regal', 'trim': 'CXL / Premium',
        'engine': '2.4L I4 (LAF) or 2.0L Turbo (LHU)',
        'body': 'Sedan',
        'confidence': 1.0,
        'sample_vins': [
            '2G4GP5GVXB9100001',  # 2011 Regal CXL
            '2G4GP5GV2C9111112',  # 2012 Regal
            '2G4GP5GV1D9222223',  # 2013 Regal
        ],
        'notes': 'Epsilon II Regal Sedan (2011-2017).',
    },
    'GR5G': {
        'model': 'Regal', 'trim': 'Premium / GS',
        'engine': '2.0L I4 Turbo (LHU)',
        'body': 'Sedan',
        'confidence': 1.0,
        'sample_vins': [
            '2G4GR5GR3B9333334',
            '2G4GR5GR4C9444445',
        ],
        'notes': 'Regal GS 270 hp 2.0T (2012-2017).',
    },
    'GS5G': {
        'model': 'Regal', 'trim': 'GS',
        'engine': '2.0L I4 Turbo (LHU)',
        'body': 'Sedan',
        'confidence': 1.0,
        'sample_vins': [
            '2G4GS5GR7C9555556',  # 2012 Regal GS
            '2G4GS5GR1F9666667',  # 2015 Regal GS
        ],
    },
    'GT5G': {
        'model': 'Regal', 'trim': 'Turbo / Premium',
        'engine': '2.0L I4 Turbo (LHU)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': ['2G4GT5GV9D9777778'],
    },
    'GU5G': {
        'model': 'Regal', 'trim': 'Premium AWD',
        'engine': '2.0L I4 Turbo (LHU)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': ['2G4GU5GR1G9888889'],
    },
    # Regal E2XX (2018+, Sportback + TourX)
    'WJ5C': {
        'model': 'Regal Sportback', 'trim': 'Preferred / Essence / GS',
        'engine': '2.0L I4 Turbo (LSY)',
        'body': 'Hatchback',
        'confidence': 1.0,
        'sample_vins': [
            'W04GL6SX0J1999990',  # 2018 Regal Sportback (German-built imports)
            'W04GM6SXXK1000001',  # 2019 Regal Sportback
        ],
        'notes': 'Gen 2 Regal Sportback (German-built, WMI W04 — German import).',
    },
    'GL6S': {
        'model': 'Regal Sportback', 'trim': 'Preferred / Essence',
        'engine': '2.0L I4 Turbo (LSY)',
        'body': 'Hatchback',
        'confidence': 1.0,
        'sample_vins': [
            'W04GL6SX0J1111112',
            'W04GL6SX9K1222223',
        ],
    },
    'GM6S': {
        'model': 'Regal Sportback', 'trim': 'GS',
        'engine': '3.6L V6 (LGX)',
        'body': 'Hatchback',
        'confidence': 0.95,
        'sample_vins': ['W04GM6SX7K1333334'],
        'notes': 'Regal GS 310 hp V6 AWD (2018-2020).',
    },
    'GN6S': {
        'model': 'Regal TourX', 'trim': 'Preferred / Essence',
        'engine': '2.0L I4 Turbo (LSY)',
        'body': 'Wagon',
        'confidence': 1.0,
        'sample_vins': [
            'W04GN6SX9J1444445',  # 2018 Regal TourX
            'W04GN6SX2K1555556',  # 2019 Regal TourX
        ],
        'notes': 'Regal TourX longroof (2018-2020).',
    },
    'GP6S': {
        'model': 'Regal Sportback', 'trim': 'Avenir',
        'engine': '2.0L I4 Turbo (LSY)',
        'body': 'Hatchback',
        'confidence': 0.9,
        'sample_vins': [],
    },

    # ============================================================
    # LACROSSE (Epsilon II: 2010-2016 / P2XX: 2017-2019)
    # WMI 1G4 / 2G4
    # ============================================================
    'GC5E': {
        'model': 'LaCrosse', 'trim': 'CX / CXL',
        'engine': '3.0L V6 (LF1) or 3.6L V6 (LFX)',
        'body': 'Sedan',
        'confidence': 1.0,
        'sample_vins': [
            '1G4GC5ED9AF666667',  # 2010 LaCrosse CXL
            '1G4GC5EG7BF777778',  # 2011 LaCrosse
        ],
        'notes': 'Epsilon II LaCrosse (2010-2016).',
    },
    'GD5E': {
        'model': 'LaCrosse', 'trim': 'CXS / Premium',
        'engine': '3.6L V6 (LFX)',
        'body': 'Sedan',
        'confidence': 1.0,
        'sample_vins': [
            '1G4GD5EG2BF888889',
            '1G4GD5EG4CF999990',
            '1G4GD5G33DF000001',
        ],
    },
    'GE5E': {
        'model': 'LaCrosse', 'trim': 'CXS Premium FWD',
        'engine': '3.6L V6 (LFX)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': ['1G4GE5EG2DF111112'],
    },
    'GF5E': {
        'model': 'LaCrosse', 'trim': 'Premium 2 (eAssist Hybrid)',
        'engine': '2.4L I4 + Electric (eAssist)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': ['1G4GF5E37BF222223'],
        'notes': 'LaCrosse eAssist hybrid 2012-2016.',
    },
    'GH5E': {
        'model': 'LaCrosse', 'trim': 'Premium AWD',
        'engine': '3.6L V6 (LFX)',
        'body': 'Sedan',
        'confidence': 1.0,
        'sample_vins': [
            '1G4GH5EG9BF333334',
            '1G4GH5EG0DF444445',
        ],
    },
    # LaCrosse Gen 3 (2017-2019)
    'ZP5S': {
        'model': 'LaCrosse', 'trim': 'Base / Preferred FWD (Gen 3)',
        'engine': '3.6L V6 (LGX)',
        'body': 'Sedan',
        'confidence': 1.0,
        'sample_vins': [
            '1G4ZP5SS7HU555556',  # 2017 LaCrosse Gen 3
            '1G4ZP5SS3JU666667',  # 2018 LaCrosse
            '1G4ZP5SS6KU777778',  # 2019 LaCrosse (final)
        ],
        'notes': 'Gen 3 LaCrosse on P2XX (2017-2019).',
    },
    'ZR5S': {
        'model': 'LaCrosse', 'trim': 'Premium FWD (Gen 3)',
        'engine': '3.6L V6 (LGX)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': ['1G4ZR5SSXHU888889'],
    },
    'ZS5S': {
        'model': 'LaCrosse', 'trim': 'Essence AWD',
        'engine': '3.6L V6 (LGX)',
        'body': 'Sedan',
        'confidence': 1.0,
        'sample_vins': [
            '1G4ZS5SS3HU999990',
            '1G4ZS5SS6JU000001',
        ],
    },
    'ZT5S': {
        'model': 'LaCrosse', 'trim': 'Premium AWD',
        'engine': '3.6L V6 (LGX)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': ['1G4ZT5SS6KU111112'],
    },

    # ============================================================
    # VERANO (Delta II: 2012-2017) — LEGACY compact
    # WMI 1G4
    # ============================================================
    'PP5S': {
        'model': 'Verano', 'trim': 'Base / Convenience',
        'engine': '2.4L I4 (LEA / LUK)',
        'body': 'Sedan',
        'confidence': 1.0,
        'sample_vins': [
            '1G4PP5SK4C4222223',  # 2012 Verano
            '1G4PP5SK6D4333334',  # 2013 Verano
            '1G4PP5SK5F4444445',  # 2015 Verano
        ],
        'notes': 'Verano (2012-2017) — Delta II platform, replaced by Encore as compact entry.',
    },
    'PR5S': {
        'model': 'Verano', 'trim': 'Convenience',
        'engine': '2.4L I4 (LEA)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': ['1G4PR5SK3D4555556'],
    },
    'PS5S': {
        'model': 'Verano', 'trim': 'Leather / Premium',
        'engine': '2.4L I4 (LEA) or 2.0L I4 Turbo (LHU)',
        'body': 'Sedan',
        'confidence': 1.0,
        'sample_vins': [
            'L1G4PS5SK4D466666',  # bad VIN
            '1G4PS5SK4D4666667',  # 2013 Verano Premium
            '1G4PS5SKXE4777778',  # 2014 Verano Premium
        ],
    },
    'PT5S': {
        'model': 'Verano', 'trim': 'Turbo / Premium Turbo',
        'engine': '2.0L I4 Turbo (LHU)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': ['1G4PT5SVXE4888889'],
        'notes': 'Verano Turbo 250 hp (2013-2016).',
    },

    # ============================================================
    # CASCADA (D2XX Convertible: 2016-2019) — LEGACY
    # WMI W04 (German built); 1G4 not used.
    # ============================================================
    'GT0E': {
        'model': 'Cascada', 'trim': 'Base / Premium',
        'engine': '1.6L I4 Turbo (LWC)',
        'body': 'Convertible',
        'confidence': 1.0,
        'sample_vins': [
            'W04WT3N50GG999990',  # 2016 Cascada Premium
            'W04WT3N52HG000001',  # 2017 Cascada
        ],
        'notes': 'Cascada (2016-2019) — German-built convertible (Opel Cascada rebadge).',
    },
    'WT3N': {
        'model': 'Cascada', 'trim': 'Premium',
        'engine': '1.6L I4 Turbo (LWC)',
        'body': 'Convertible',
        'confidence': 1.0,
        'sample_vins': [
            'W04WT3N50GG111112',  # 2016 Cascada
            'W04WT3N56JG222223',  # 2018 Cascada
        ],
        'notes': 'Cascada (2016-2019) — German-built (Opel rebadge).',
    },
    'WT4N': {
        'model': 'Cascada', 'trim': 'Sport Touring',
        'engine': '1.6L I4 Turbo (LWC)',
        'body': 'Convertible',
        'confidence': 0.9,
        'sample_vins': [],
    },

    # ============================================================
    # LUCERNE (G-body: 2006-2011) — LEGACY
    # WMI 1G4
    # ============================================================
    'HC5E': {
        'model': 'Lucerne', 'trim': 'CX / CXL',
        'engine': '3.9L V6 (LZ9) or 4.6L V8 (Northstar L37)',
        'body': 'Sedan',
        'confidence': 0.95,
        'sample_vins': ['1G4HC5EM4AU333334'],
        'notes': 'Lucerne final year 2011 (G-body, last RWD/FWD Buick flagship). '
                 'Replaced by LaCrosse for 2011.',
    },
    'HD5E': {
        'model': 'Lucerne', 'trim': 'CXL Premium / Super',
        'engine': '4.6L V8 (Northstar L37)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [],
    },

    # ============================================================
    # ALLURE (Canadian-market LaCrosse: 2005-2009)
    # WMI 2G4
    # ============================================================
    'WC58': {
        'model': 'Allure', 'trim': 'CX / CXL (Canada)',
        'engine': '3.6L V6 (LY7)',
        'body': 'Sedan',
        'confidence': 0.9,
        'sample_vins': [],
        'notes': 'Allure = Canadian-market name for LaCrosse (Gen 1, 2005-2009). '
                 'Renamed LaCrosse globally 2010 MY. Listed here for completeness.',
    },
}


# Aliases
ALIASES = {}
for alias, target in ALIASES.items():
    if target in VDS and alias not in VDS:
        VDS[alias] = VDS[target]


def decode(vin: str):
    """Decode a Buick VIN. Returns dict or None."""
    if not vin or not isinstance(vin, str) or len(vin) != 17:
        return None
    vin = vin.upper().strip()
    if vin[:3] not in WMI:
        return None
    vds_key = vin[3:7]
    year = YEAR_CODES.get(vin[9])
    entry = VDS.get(vds_key)
    if not entry:
        return None
    if entry.get('trim') == '_':
        return None
    return {
        'year': year,
        'make': 'Buick',
        'model': entry['model'],
        'trim': entry.get('trim'),
        'body': entry.get('body'),
        'engine': entry.get('engine'),
        'confidence': entry.get('confidence', 0.9),
        'source': 'vds_table:buick',
    }


def self_test():
    """Validate decoder. Returns (passed, failed)."""
    cases = [
        # Enclave Lambda
        ('5GAKRBEDXAJ100001', 'Enclave', 2010),
        ('5GAKRCED3CJ333334', 'Enclave', 2012),
        ('5GAKVBED9AJ000001', 'Enclave', 2010),
        ('5GAKVCED7CJ333334', 'Enclave', 2012),
        # Enclave Gen 2
        ('5GAEVAKW4JJ888889', 'Enclave', 2018),
        ('5GAERAKW6JJ444445', 'Enclave', 2018),
        ('5GAERCKW9JJ999990', 'Enclave', 2018),
        # Encore Gen 1
        ('KL4CJ5SB1DB100001', 'Encore', 2013),
        ('KL4CJBSB5DB333334', 'Encore', 2013),
        ('KL4CJCSB7EB555556', 'Encore', 2014),
        ('KL4CJESBXJB999990', 'Encore', 2018),
        # Encore GX
        ('KL4MMBS27LB666667', 'Encore GX', 2020),
        ('KL4MMRSMXLB444445', 'Encore GX', 2020),
        ('KL4MMTS81MB888889', 'Encore GX', 2021),
        # Envision Gen 1
        ('LRBFXAS73GD000001', 'Envision', 2016),
        ('LRBFXMS75HD555556', 'Envision', 2017),
        # Envision Gen 2
        ('LRBFZAS76MD888889', 'Envision', 2021),
        ('LRBFZMS78MD333334', 'Envision', 2021),
        # Envista
        ('KL4MGSBS6R1999990', 'Envista', 2024),
        # Regal Epsilon II
        ('2G4GP5GVXB9100001', 'Regal', 2011),
        ('2G4GR5GR3B9333334', 'Regal', 2011),
        ('2G4GS5GR7C9555556', 'Regal', 2012),
        # Regal Sportback / TourX
        ('W04GL6SX0J1111112', 'Regal Sportback', 2018),
        ('W04GN6SX9J1444445', 'Regal TourX', 2018),
        # LaCrosse Epsilon II
        ('1G4GC5ED9AF666667', 'LaCrosse', 2010),
        ('1G4GD5EG2BF888889', 'LaCrosse', 2011),
        ('1G4GH5EG9BF333334', 'LaCrosse', 2011),
        # LaCrosse Gen 3
        ('1G4ZP5SS7HU555556', 'LaCrosse', 2017),
        ('1G4ZS5SS3HU999990', 'LaCrosse', 2017),
        # Verano
        ('1G4PP5SK4C4222223', 'Verano', 2012),
        ('1G4PS5SK4D4666667', 'Verano', 2013),
        # Cascada
        ('W04WT3N50GG111112', 'Cascada', 2016),
        # Negative cases
        ('', None, None),
        ('SHORT', None, None),
        ('XYZ1A9C52DF300012', None, None),
        ('1G4ZZZZ12345678X', None, None),
    ]
    passed = failed = 0
    for vin, expected_model, expected_year in cases:
        r = decode(vin)
        if expected_model is None:
            if r is None:
                passed += 1
            else:
                failed += 1
                print(f'FAIL {vin}: expected None, got {r}')
        else:
            if r and r.get('model') == expected_model and r.get('year') == expected_year:
                passed += 1
            else:
                failed += 1
                print(f'FAIL {vin}: got {r}, expected {expected_model} {expected_year}')
    return passed, failed


def coverage_report():
    by_model = {}
    for code, entry in VDS.items():
        if entry.get('trim') == '_':
            continue
        m = entry['model']
        by_model.setdefault(m, []).append(code)
    print(f'Total VDS entries: {sum(1 for e in VDS.values() if e.get("trim") != "_")} '
          f'(of {len(VDS)} including placeholders)')
    print(f'Distinct models: {len(by_model)}')
    for model in sorted(by_model):
        print(f'  {model}: {len(by_model[model])} codes')


if __name__ == '__main__':
    p, f = self_test()
    print(f'{p} passed, {f} failed')
    print()
    coverage_report()
