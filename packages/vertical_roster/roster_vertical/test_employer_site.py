"""The employer's own website, read from their board page (fixtures mirror the real pages' shape)."""
from roster_vertical.employer_site import company_site, from_ashby, from_header_link

ASHBY = ('<html><script nonce="x">window.__appData = {"environment":"production","organization":'
         '{"organizationId":"f490","name":"Profound","publicWebsite":"https://www.tryprofound.com/","customJobsPageUrl":null},'
         '"theme":{"logo":"https://app.ashbyhq.com/api/images/org-theme-logo/f490.png"}};</script></html>')
LEVER = ('<html><head><link rel="icon" href="https://cdn.lever.co/favicon.png"></head><body>'
         '<a href="https://fullscript.com"><img src="logo.png"></a>'
         '<a href="https://www.linkedin.com/company/fullscript">LinkedIn</a></body></html>')


def test_ashby_gives_the_organizations_public_website():
    assert from_ashby(ASHBY) == "https://www.tryprofound.com"
    assert company_site("https://jobs.ashbyhq.com/profound", ASHBY) == "https://www.tryprofound.com"
    assert from_ashby('<html>no app data here</html>') is None
    assert from_ashby('<script>window.__appData = {"organization":{"publicWebsite":""}};</script>') is None


def test_a_header_link_gives_the_site_and_the_ats_or_a_social_link_never_does():
    assert from_header_link(LEVER) == "https://fullscript.com"
    assert company_site("https://jobs.lever.co/fullscript", LEVER) == "https://fullscript.com"
    assert from_header_link('<a href="https://jobs.ashbyhq.com/acme">board</a><a href="https://x.com/acme">x</a>') is None
    assert from_header_link('<a href="https://cdn.ashbyprd.com/assets/f.svg">asset</a>') is None


def test_an_unknown_board_yields_nothing():
    assert company_site("https://apply.workable.com/j/ABC", "<a href='https://acme.com'>x</a>") is None
    assert company_site("", ASHBY) is None
