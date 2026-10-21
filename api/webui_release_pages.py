"""Human-facing published WebUI Release index; source tags are not releases."""
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlparse
import urllib.request


class ReleasePage(HTMLParser):
    def __init__(self, repository):
        super().__init__();self.repository=repository;self.releases=[];self.section=None
        self.notes=False;self.header=[];self.next=None;self.seen_section=False

    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag=='section' and a.get('data-release-anchor','').startswith('release-'):
            self.seen_section=True;self.section={'name':a['data-release-anchor'][8:],'sha':None,'link':False}
            self.notes=False;self.header=[]
        if self.section is not None:
            if 'markdown-body' in a.get('class','').split():self.notes=True
            href=a.get('href','')
            if not self.notes:
                if href==f"/{self.repository}/releases/tag/{self.section['name']}":self.section['link']=True
                m=re.fullmatch('/'+re.escape(self.repository)+r'/commit/([0-9a-f]{40})',href)
                if m:self.section['sha']=m[1]
        if tag=='a' and 'next' in a.get('rel','').split():self.next=a.get('href')

    def handle_data(self, text):
        if self.section is not None and not self.notes:self.header.append(text)

    def handle_endtag(self, tag):
        if tag=='section' and self.section is not None:
            item=self.section
            if item['link']:
                item['prerelease']=bool(re.search(r'\bPre-release\b',' '.join(self.header),re.I))
                self.releases.append(item)
            self.section=None


def published(repository, channel='stable', timeout=5):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository):
        raise ValueError('Invalid Release repository')
    base='https://github.com/'+repository+'/releases';url=base;seen=set();items={}
    for _ in range(10):
        if url in seen:raise ValueError('Release pagination repeated')
        seen.add(url)
        request=urllib.request.Request(url,headers={'User-Agent':'hermes-webui','Accept':'text/html'})
        with urllib.request.urlopen(request,timeout=timeout) as r:
            final=r.geturl() if hasattr(r,'geturl') else url
            if final!=url:raise ValueError('Unverified Release redirect')
            raw=r.read(2_000_001)
        if len(raw)>2_000_000:raise ValueError('Release page too large')
        parser=ReleasePage(repository);parser.feed(raw.decode('utf-8'))
        if not parser.seen_section:raise ValueError('Published Release sections unavailable')
        for item in parser.releases:
            name=item['name'];experimental=channel=='experimental'
            pattern=r'exp-v[0-9][0-9A-Za-z.+-]*' if experimental else r'v[0-9][0-9A-Za-z.+-]*'
            if bool(item['prerelease'])==experimental and re.fullmatch(pattern,name):
                items[name]={'name':name,'sha':item['sha'] or name}
        if not parser.next:break
        next_url=urljoin(base,parser.next);parts=urlparse(next_url)
        if (parts.scheme!='https' or parts.netloc!='github.com' or parts.path!=urlparse(base).path
                or not re.fullmatch(r'page=[1-9][0-9]*',parts.query) or parts.fragment):
            raise ValueError('Unverified Release pagination')
        url=next_url
    else:
        raise ValueError('Release pagination exceeds bounded history')
    return list(items.values())
