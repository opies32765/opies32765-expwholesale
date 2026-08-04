#!/usr/bin/env python3
"""dp_name_map.py — work out a greeting for every staged outreach address.

The campaign copy opens "Hi {first}," but dp_outreach_targets.name holds the
DEALERSHIP (Autobuy, Marshall Goldman Motor Sls & Leasing Llc), never a person.
Every other source was checked and came back empty:

    contacts             matched on phone -> 0   (that table is bidders)
    dealer_applications  matched on email -> 0   (these 736 are cold)
    dealerprice_members  matched on email -> 0   (none are members yet)

So the email local-part is the only signal available. This classifies all 736
and is deliberately CONSERVATIVE: it would rather return no name than address
a dealer by the wrong one. "Hi Bnorton," on a cold email is worse than no
greeting at all -- it advertises a broken mail merge.

Buckets, most to least confident:

  dot_known     first.last@ where the first token is a known given name
                -> Chris, Roberto, Tyler. Safe.
  single_known  a single token that is a known given name
                -> ed, shane, mohsen. Safe.
  dot_unknown   first.last@ but the first token is not in the dictionary
                -> probably still a given name (an unusual or non-Anglo one),
                   but unverified. NEEDS A HUMAN EYE.
  initial_last  a single token that looks like initial+surname
                -> bnorton, cwegmann, dmaher, jconroy. The first name is NOT
                   PRESENT IN THE STRING. Not recoverable. Do not guess.
  role          info@, sales@, used@ ... a desk, not a person.
  company       dsmotorsflorida, hollywoodmotorsusa ... the business.
  unknown       everything else.

Only dot_known and single_known should merge automatically. Everything else
gets the fallback greeting unless someone fills it in by hand.
"""
import io
import os
import re
import sys
import csv
import psycopg2
import psycopg2.extras

# Common US given names. Not exhaustive -- it does not need to be. A miss costs
# a fallback greeting; a false positive costs a dealer being called by a name
# that is not theirs, so the list stays tight and conventional.
NAMES = set("""
aaron abby abdul abel abraham adam adrian adriana ahmad ahmed aidan al alan
albert alberto alec alejandro alex alexander alexandra alexis alfred alfredo
ali alicia alison allan allen allison alvin amanda amber amy ana anastasia
andre andrea andres andrew andy angel angela angelo anita ann anna anne
annette anthony antoine antonio april arlene armando arnold arsen art arthur
ashley aubrey audrey austin barbara barry beatrice becky ben benjamin bernard
beth bethany betty beverly bill billy blake bob bobby bonnie brad bradley
brandon brandy brenda brendan brent brett brian briana bridget brittany brooke
bruce bryan bryce byron caleb calvin cameron candace carl carla carlos carmen
carol carole caroline carolyn carrie casey cassandra catherine cathy cecil
cedric chad charlene charles charlie charlotte chase chelsea cheryl chester
chris christian christina christine christopher chuck cindy claire clara
clarence claude clay clayton clifford clint clinton clyde cody colby cole
colin colleen connie connor conrad corey cory courtney craig cristian crystal
curtis cynthia dale dallas dalton damon dan dana daniel danielle danny darin
darius darla darlene darnell darrel darrell darren darryl daryl dave david
dawn dean deanna debbie deborah debra dennis denise derek derrick desmond
devin devon diana diane dianne dick diego dillon dominic dominique don donald
donna donnie doris dorothy doug douglas drew duane dustin dwayne dwight dylan
earl ed eddie eden edgar edith edward edwin eileen elaine eleanor elena eli
elijah elizabeth ellen elliot elmer elsa elvis emanuel emily emma eric erica
erik erin ernest ernesto errol ervin esther ethan eugene eva evan evelyn
everett fabian faith felicia felipe felix fernando florence floyd forrest
frances francis francisco frank franklin fred freddie frederick gabriel gail
gary gavin gene geoffrey george gerald geraldine gerard german gilbert gina
ginger glen glenn gloria gordon grace grant greg gregg gregory gretchen
guadalupe guillermo gus guy hank hannah harold harry harvey hassan heather
hector heidi helen henry herbert herman hilda holly homer hope horace howard
hugh hugo ian ida ignacio irene iris irma isaac isabel ismael israel ivan
jack jackie jackson jacob jacqueline jaime jake james jamie jan jane janet
janice jared jason javier jay jayson jean jeanette jeanne jeff jeffery
jeffrey jenna jennifer jenny jeremiah jeremy jermaine jerome jerry jesse
jessica jesus jill jim jimmy joan joann joanna joaquin jody joe joel joey
john johnny jon jonathan jordan jorge jose joseph josh joshua joy joyce juan
juanita judith judy julia julian julie julio justin kaitlin kara karen kari
karl kate katherine kathleen kathryn kathy katie katrina keith kelly kelvin
ken kendra kenneth kenny kent kerry kevin kim kimberly kirk kris krista
kristen kristin kristina kristy kurt kyle lamar lance larry latoya laura
lauren laurie lawrence lee leo leon leonard leroy leslie lester lewis liliana
lillian linda lindsay lindsey lionel lisa lloyd logan lois lonnie loren
lorenzo loretta lori lorraine louis louise lucas lucy luis luke luther lydia
lyle lynn mack madison mae maggie malcolm manuel marc marcel marco marcos
marcus margaret maria mariah marian marie marilyn mario marion marisa mark
marlon marsha marshall martha martin marty marvin mary mason mathew matt
matthew maureen maurice max maxine megan meghan melanie melinda melissa
melvin mercedes meredith mia micah michael micheal michele michelle miguel
mike mildred miles milton mindy miranda miriam misty mitch mitchell mohamed
mohammed mohsen monica monique morgan morris moses muhammad murray myron nadia
nancy naomi natalie nathan nathaniel neal neil nelson nicholas nick nicolas
nicole noah noel nora norma norman octavio olga oliver olivia omar oscar
otis owen pablo paige pam pamela pat patricia patrick patti paul paula
pauline pearl pedro peggy penny percy perry pete peter phil philip phillip
phyllis pierre preston priscilla quentin quincy rachel rafael ralph ramon
randall randy raul ray raymond rebecca reggie regina reginald rene renee
rex rhonda ricardo richard rick rickey ricky rita rob robert roberta roberto
robin rocco rod rodney rodolfo roger roland roman ron ronald ronnie rosa
rose rosemary ross roy ruben ruby rudy russell ruth ryan sabrina sally
salvador sam samantha sammy samuel sandra sandy santiago sara sarah saul
scott sean sebastian selena serena sergio seth shane shannon shari sharon
shaun shawn sheila shelly sherri sherry sheryl shirley sidney silvia simon
sonia sonya sophia spencer stacey stacy stan stanley stella stephanie stephen
steve steven stuart sue susan suzanne sydney sylvia tabitha tamara tammy tara
ted teresa terrance terrence terri terry thelma theodore theresa thomas tiffany
tim timothy tina toby todd tom tommy tony tonya tracey tracy travis trent
trevor tricia troy tyler tyrone valerie vance vanessa vaughn velma vera vernon
veronica vicki vickie victor victoria vince vincent viola violet virgil
virginia vivian wade wallace walter wanda warren wayne wendell wendy wesley
whitney wilbur wiley will william willie willis wilson winston yolanda
yvette yvonne zachary zachery
""".split())

ROLE = set("""
info sales used usedcars newcars admin office contact accounting accounts
parts service finance gm gsm internet leads lead team desk manager mgr title
titles buyer buying purchasing wholesale inventory dealer dealers cars auto
autos motors sale support billing ap ar hr payroll web webmaster marketing
reception frontdesk general main mail email orders customerservice cs
""".split())

COMPANYISH = ('motors', 'motor', 'auto', 'autos', 'cars', 'car', 'sales',
              'inc', 'llc', 'group', 'usa', 'performance', 'wholesale',
              'imports', 'import', 'export', 'exotics', 'leasing', 'rv',
              'trucks', 'truck', 'dealership', 'enterprise', 'company')


def classify(local):
    """Return (bucket, first_name_or_empty)."""
    raw = (local or '').strip().lower()
    if not raw:
        return 'empty', ''
    # strip trailing digits: debraales1963 -> debraales
    core = re.sub(r'\d+$', '', raw)
    if core in ROLE or raw in ROLE:
        return 'role', ''

    if '.' in core or '_' in core:
        head = re.split(r'[._]', core)[0]
        head = re.sub(r'[^a-z]', '', head)
        if head in NAMES:
            return 'dot_known', head.capitalize()
        if len(head) >= 3 and head not in ROLE:
            return 'dot_unknown', head.capitalize()
        return 'unknown', ''

    token = re.sub(r'[^a-z]', '', core)
    if not token:
        return 'unknown', ''
    if token in NAMES:
        return 'single_known', token.capitalize()
    if any(c in token for c in COMPANYISH) and len(token) >= 9:
        return 'company', ''
    # bnorton / cwegmann / dmaher -- drop the leading initial and see if what
    # is left is a plausible surname. The FIRST NAME IS NOT IN THE STRING.
    if 3 <= len(token) <= 14 and token[1:] not in NAMES:
        return 'initial_last', ''
    return 'unknown', ''


def main():
    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        print('DATABASE_URL is not set. Refusing to guess credentials.')
        return 2
    c = psycopg2.connect(dsn)
    cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT id, name, email, total_profit
                     FROM dp_outreach_targets
                    WHERE removed_at IS NULL AND email IS NOT NULL AND email<>''
                    ORDER BY total_profit DESC NULLS LAST""")
    rows = cur.fetchall()
    c.close()

    buckets = {}
    out = []
    for r in rows:
        local = (r['email'] or '').split('@')[0]
        b, first = classify(local)
        buckets.setdefault(b, []).append((r['email'], first))
        out.append({'id': r['id'], 'dealership': r['name'], 'email': r['email'],
                    'bucket': b, 'first_name': first,
                    'auto_merge': 'yes' if b in ('dot_known', 'single_known') else 'no',
                    'total_profit': r['total_profit']})

    path = '/tmp/dp_name_map.csv'
    with io.open(path, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['id', 'dealership', 'email', 'bucket',
                                          'first_name', 'auto_merge',
                                          'total_profit'])
        w.writeheader()
        w.writerows(out)

    total = len(rows)
    order = ['dot_known', 'single_known', 'dot_unknown', 'initial_last',
             'role', 'company', 'unknown', 'empty']
    print('TOTAL STAGED: %d\n' % total)
    print('%-14s %6s %7s   %s' % ('BUCKET', 'COUNT', 'SHARE', 'EXAMPLES'))
    print('-' * 78)
    auto = 0
    for b in order:
        v = buckets.get(b) or []
        if not v:
            continue
        if b in ('dot_known', 'single_known'):
            auto += len(v)
        ex = ', '.join(('%s->%s' % (e.split('@')[0][:16], n or '-')) for e, n in v[:3])
        print('%-14s %6d %6.1f%%   %s' % (b, len(v), 100.0 * len(v) / total, ex))
    print('-' * 78)
    print('AUTO-MERGEABLE (safe)     : %d  (%.1f%%)' % (auto, 100.0 * auto / total))
    print('NEEDS FALLBACK OR A HUMAN : %d  (%.1f%%)' % (total - auto,
                                                        100.0 * (total - auto) / total))
    print('\nwrote %s' % path)


if __name__ == '__main__':
    sys.exit(main())
