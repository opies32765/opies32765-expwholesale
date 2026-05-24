"""EW outbound dialer — places a Twilio voice call to a partner dealer
that bridges to our Bill voice bot.

CLI:
  /opt/expwholesale/venv/bin/python3 /opt/expwholesale/ew_dialer.py \\
      --bid 2009 --to +1XXXXXXXXXX --partner 'TXT Charlie' --score 87

Reads Twilio creds from /etc/default/expwholesale (already loaded for
expwholesale.service). Uses the same TWILIO_PHONE as SMS — outbound
voice is a parallel capability, won't interfere with SMS routes."""
import argparse, os, sys, urllib.parse

def main():
    parser = argparse.ArgumentParser(description="EW outbound voice dialer")
    parser.add_argument("--bid",     required=True, help="Bid ID to pitch")
    parser.add_argument("--to",      required=True, help="Partner phone +1XXXXXXXXXX")
    parser.add_argument("--partner", required=True, help="Partner display name (e.g. 'TXT Charlie')")
    parser.add_argument("--score",   default="",    help="Match score 0-100 (optional)")
    parser.add_argument("--dry-run", action="store_true", help="Print Twilio params, don't dial")
    args = parser.parse_args()

    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    tok = os.environ.get("TWILIO_AUTH_TOKEN")
    frm = os.environ.get("TWILIO_PHONE")
    if not (sid and tok and frm):
        print("ERROR: missing TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/TWILIO_PHONE in env")
        sys.exit(1)

    # Build the TwiML URL with embedded params
    q = urllib.parse.urlencode({
        "bid_id":         args.bid,
        "partner_name":   args.partner,
        "partner_phone":  args.to,
        "match_score":    args.score,
    })
    twiml_url = f"https://voice.experience-wholesale.net/twiml/ew-outbound?{q}"

    if args.dry_run:
        print(f"DRY-RUN — would dial:")
        print(f"  from: {frm}")
        print(f"  to:   {args.to}")
        print(f"  url:  {twiml_url}")
        print(f"  AMD:  Enable (DetectMessageEnd)")
        sys.exit(0)

    from twilio.rest import Client
    client = Client(sid, tok)
    call = client.calls.create(
        to=args.to,
        from_=frm,
        url=twiml_url,
        # Answering Machine Detection — Twilio determines if a human or
        # machine picked up before connecting our bot. AMD adds 1-3s of
        # initial latency but prevents Bill from pitching a voicemail.
        machine_detection="DetectMessageEnd",
        machine_detection_timeout=5,
        # If the call is unanswered/busy/no-answer, give up after 30s
        timeout=30,
        # Status callbacks to log call outcomes — optional
        # status_callback="https://experience-wholesale.net/twilio/call-status",
        # status_callback_event=["initiated","ringing","answered","completed"],
    )
    print(f"dialed: callSid={call.sid} status={call.status}")
    print(f"  to:   {args.to}")
    print(f"  from: {frm}")
    print(f"  bid:  {args.bid}  partner: {args.partner}  score: {args.score}")

if __name__ == "__main__":
    main()
