from __future__ import annotations

import pytest

from app.services import academic as academic_module
from app.services import crawler as crawler_mod
from app.services.academic import (
    AcademicPaper,
    Section,
    academic_pdf_candidates,
    canonicalize_academic_url,
    classify_academic_url,
    extract_academic_doi,
    extract_long_html,
    normalize_doi,
)
from app.services.fetcher import FetchResult


@pytest.mark.parametrize(
    ("url", "source", "canonical"),
    [
        (
            "https://export.arxiv.org/pdf/1706.03762v7.pdf?download=1#page=2",
            "arxiv",
            "https://arxiv.org/abs/1706.03762v7",
        ),
        (
            "http://arxiv.org/abs/hep-th/9901001",
            "arxiv",
            "https://arxiv.org/abs/hep-th/9901001",
        ),
        (
            "https://www.ncbi.nlm.nih.gov/pubmed/31452104?report=abstract",
            "pubmed",
            "https://pubmed.ncbi.nlm.nih.gov/31452104/",
        ),
        (
            "https://www.ncbi.nlm.nih.gov/pmc/articles/pmc6775758/?tool=pmcentrez",
            "pmc",
            "https://pmc.ncbi.nlm.nih.gov/articles/PMC6775758/",
        ),
        (
            "http://dx.doi.org/10.1038/s41586-021-03819-2?via=ihub",
            "doi",
            "https://doi.org/10.1038/s41586-021-03819-2",
        ),
        (
            "https://github.com/Owner/repository.git#readme",
            "github",
            "https://github.com/Owner/repository",
        ),
    ],
)
def test_academic_url_classification_and_canonicalization(
    url: str,
    source: str,
    canonical: str,
) -> None:
    assert classify_academic_url(url) == source
    assert canonicalize_academic_url(url) == canonical


def test_academic_url_classification_uses_hostname_boundaries() -> None:
    assert classify_academic_url("https://arxiv.org.attacker.example/abs/1706.03762") is None
    disguised = "https://example.com/?next=https://pubmed.ncbi.nlm.nih.gov/1"
    assert classify_academic_url(disguised) is None


def test_journal_homepage_alone_is_not_misrouted_as_a_paper() -> None:
    html = "<html><head><title>Nature</title></head><body>Browse journals</body></html>"

    assert classify_academic_url("https://www.nature.com/") == "journal"
    assert not crawler_mod._is_academic_content(html, "https://www.nature.com/")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("doi:10.1000/xyz123.", "10.1000/xyz123"),
        ("https://doi.org/10.5555/ABC.DEF)", "10.5555/ABC.DEF"),
        (
            "identifier 10.1002/(SICI)1099-0844(199912)17:4"
            "<290::AID-CBF849>3.0.CO;2-P",
            "10.1002/(SICI)1099-0844(199912)17:4<290::AID-CBF849>3.0.CO;2-P",
        ),
        ("not a doi", ""),
    ],
)
def test_normalize_doi(raw: str, expected: str) -> None:
    assert normalize_doi(raw) == expected


@pytest.mark.parametrize(
    ("url", "doi"),
    [
        (
            "https://onlinelibrary.wiley.com/doi/10.1002/cae.22725",
            "10.1002/cae.22725",
        ),
        (
            "https://www.science.org/doi/10.1126/science.1225829",
            "10.1126/science.1225829",
        ),
        (
            "https://link.springer.com/article/10.1007/s10994-022-06200-0",
            "10.1007/s10994-022-06200-0",
        ),
    ],
)
def test_academic_doi_is_extracted_from_known_publisher_paths(
    url: str,
    doi: str,
) -> None:
    assert extract_academic_doi(url) == doi


def test_academic_doi_ignores_unknown_hosts_queries_and_visible_prose() -> None:
    injected = "https://example.test/paper/10.1002/cae.22725"
    query = "https://www.wiley.com/?next=https://doi.org/10.1126/science.1225829"
    prose = "<main>A citation mentions DOI 10.1002/cae.22725.</main>"

    assert extract_academic_doi(injected) == ""
    assert extract_academic_doi(query) == ""
    assert extract_academic_doi("https://www.science.org/article/news", prose) == ""


def test_academic_doi_uses_only_structured_html_on_known_publisher() -> None:
    html = """
    <html><head>
      <meta name="citation_doi" content="doi:10.1016/j.future.2024.01.007">
    </head><body>
      Visible prose cites an unrelated 10.9999/untrusted.example.
    </body></html>
    """

    assert extract_academic_doi(
        "https://www.sciencedirect.com/science/article/pii/S0167739X24000001",
        html,
    ) == "10.1016/j.future.2024.01.007"


def test_pdf_candidates_prefer_advertised_url_and_normalize_arxiv_fallback() -> None:
    html = """
    <html><head>
      <meta NAME="Citation_PDF_URL" content="/pdf/1706.03762v7">
      <link rel="alternate" type="application/pdf" href="/pdf/duplicate.pdf">
    </head><body>
      <a href="/download/supplement.zip">Download files</a>
    </body></html>
    """

    candidates = academic_pdf_candidates(
        html,
        "https://arxiv.org/abs/1706.03762v7?context=cs#details",
    )

    assert candidates == [
        "https://arxiv.org/pdf/1706.03762v7",
        "https://arxiv.org/pdf/duplicate.pdf",
    ]


def test_preprint_pdf_candidates_have_one_deduplicated_attempt_budget() -> None:
    html = """
    <html><head>
      <meta name="citation_pdf_url" content="/content/10.1101/2024.01.02.full.pdf?download=1">
      <link type="application/pdf" href="/content/10.1101/2024.01.02.full.pdf">
    </head><body>
      <a href="/content/10.1101/2024.01.02.full.pdf?download=true">Download PDF</a>
      <a href="/content/10.1101/2024.01.02.supplement.pdf">Supplementary PDF</a>
    </body></html>
    """

    assert academic_pdf_candidates(
        html,
        "https://www.biorxiv.org/content/10.1101/2024.01.02v1",
    ) == [
        "https://www.biorxiv.org/content/10.1101/2024.01.02.full.pdf?download=1"
    ]


def test_highwire_pubmed_metadata_and_structured_abstract() -> None:
    html = """
    <html lang="en">
      <head>
        <meta NAME="citation_title" content="A Production-Grade Clinical Study">
        <meta name="citation_authors" content="Smith J;García M;">
        <meta name="citation_pmid" content="31452104">
        <meta name="citation_doi" content="doi:10.1000/Study.42.">
        <meta name="citation_journal_title" content="Journal of Reliable Systems">
        <meta name="citation_date" content="2026-07-01">
        <link rel="canonical" href="https://pubmed.ncbi.nlm.nih.gov/31452104/">
      </head>
      <body><main>
        <h1>A Production-Grade Clinical Study</h1>
        <section id="abstract">
          <h2>Abstract</h2>
          <h3>Background</h3><p>Prior systems lost important structured metadata.</p>
          <h3>Results</h3><p>The corrected pipeline retained it reliably.</p>
        </section>
        <h2>1 Introduction</h2>
        <p>This introduction contains enough text to represent the paper body.</p>
        <h2>References</h2>
        <p>[1] A source used by the study.</p>
      </main></body>
    </html>
    """

    paper = extract_long_html(
        html,
        "https://pubmed.ncbi.nlm.nih.gov/31452104/?format=pubmed",
    )
    markdown = paper.to_markdown()

    assert paper.title == "A Production-Grade Clinical Study"
    assert paper.authors == ["Smith J", "García M"]
    assert paper.doi == "10.1000/Study.42"
    assert paper.pmid == "31452104"
    assert paper.journal == "Journal of Reliable Systems"
    assert paper.publication_date == "2026-07-01"
    assert paper.language == "en"
    assert paper.canonical_url == "https://pubmed.ncbi.nlm.nih.gov/31452104/"
    assert paper.abstract == (
        "Background Prior systems lost important structured metadata. "
        "Results The corrected pipeline retained it reliably."
    )
    assert "This introduction contains enough text" not in markdown
    assert "Search in MeSH" not in markdown
    assert markdown.count("## Abstract") == 1


def test_pmc_full_text_keeps_body_and_does_not_duplicate_references() -> None:
    html = """
    <html><head>
      <meta name="citation_title" content="A Full Text Paper">
      <meta name="citation_pmcid" content="PMC12345">
    </head><body><main><article>
      <h1>A Full Text Paper</h1>
      <h2>Abstract</h2>
      <p>This is the concise abstract for the complete paper.</p>
      <h2>1 Introduction</h2>
      <p>This introduction contains the actual full text body.</p>
      <h2>References</h2>
      <p>[1] A source used by the study.</p>
    </article></main></body></html>
    """

    paper = extract_long_html(
        html,
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC12345/",
    )
    markdown = paper.to_markdown()

    assert paper.pmcid == "PMC12345"
    assert "This introduction contains the actual full text body." in markdown
    assert markdown.count("## References") == 1
    assert markdown.count("[1] A source used by the study.") == 1


def test_json_ld_metadata_fills_fields_missing_from_meta_tags() -> None:
    html = """
    <html><head>
      <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "mainEntity": {
          "@type": "ScholarlyArticle",
          "headline": "JSON-LD Scholarly Result",
          "author": [
            {"@type": "Person", "givenName": "Ada", "familyName": "Lovelace"},
            {"@type": "Person", "name": "Grace Hopper"}
          ],
          "abstract": "A structured abstract supplied by the publisher.",
          "datePublished": "2025-11-05",
          "license": "https://creativecommons.org/licenses/by/4.0/",
          "identifier": "https://doi.org/10.4242/example.9",
          "isPartOf": {"@type": "Periodical", "name": "Computing Letters"},
          "inLanguage": "en-US"
        }
      }
      </script>
    </head><body><article>
      <h1>Fallback title should not win</h1>
      <h2>Introduction</h2>
      <p>The semantic article body remains available to extraction.</p>
    </article></body></html>
    """

    paper = extract_long_html(html, "https://journals.example.test/articles/9")

    assert paper.title == "JSON-LD Scholarly Result"
    assert paper.authors == ["Ada Lovelace", "Grace Hopper"]
    assert paper.abstract == "A structured abstract supplied by the publisher."
    assert paper.doi == "10.4242/example.9"
    assert paper.journal == "Computing Letters"
    assert paper.publication_date == "2025-11-05"
    assert paper.license == "https://creativecommons.org/licenses/by/4.0/"
    assert paper.language == "en-US"


def test_preprint_default_january_first_prefers_real_structured_date() -> None:
    html = """
    <html><head>
      <meta name="citation_title" content="A Preprint Date">
      <meta name="citation_publication_date" content="2024/01/01">
      <script type="application/ld+json">
      {
        "@type": "ScholarlyArticle",
        "headline": "A Preprint Date",
        "datePublished": "2024-07-02"
      }
      </script>
    </head><body><article>
      <p>A useful preprint abstract and body are present.</p>
    </article></body></html>
    """

    paper = extract_long_html(
        html,
        "https://www.biorxiv.org/content/10.1101/2024.06.27.601098v1",
    )

    assert paper.publication_date == "2024-07-02"


def test_preprint_default_january_first_degrades_to_year_only() -> None:
    html = """
    <html><head>
      <meta name="citation_title" content="A Year-Only Preprint">
      <meta name="citation_publication_date" content="2024-01-01">
    </head><body><article>
      <p>A useful preprint abstract and body are present.</p>
    </article></body></html>
    """

    paper = extract_long_html(
        html,
        "https://www.medrxiv.org/content/10.1101/2024.06.27.601098v1",
    )

    assert paper.publication_date == "2024"


def test_highwire_author_list_takes_precedence_over_duplicate_json_ld_names() -> None:
    html = """
    <html><head>
      <meta name="citation_title" content="One Author List">
      <meta name="citation_author" content="Lovelace, Ada">
      <script type="application/ld+json">
      {
        "@type": "ScholarlyArticle",
        "headline": "One Author List",
        "author": [{"@type": "Person", "name": "Ada Lovelace"}]
      }
      </script>
    </head><body><article><p>Substantive paper content is present here.</p></article></body>
    </html>
    """

    paper = extract_long_html(html)

    assert paper.authors == ["Lovelace, Ada"]


def test_creative_commons_link_populates_license_when_metadata_is_absent() -> None:
    html = """
    <html><head><meta name="citation_title" content="Open Paper"></head>
    <body><article>
      <p>Open access paper body.</p>
      <p>This work uses the
        <a href="https://creativecommons.org/licenses/by/4.0/">
          Creative Commons Attribution license
        </a>.
      </p>
    </article></body></html>
    """

    paper = extract_long_html(html)

    assert paper.license == "https://creativecommons.org/licenses/by/4.0/"


def test_ieee_embedded_metadata_is_extracted_without_highwire_tags() -> None:
    html = """
    <html><head><title>Branded IEEE page</title></head><body>
      <script>
      xplGlobal.document.metadata={
        "displayDocTitle": "Reliable Skyrmion Logic",
        "authors": [{"name": "Ada Researcher"}, {"name": "Grace Scientist"}],
        "abstract": "A device reaches <i>high</i> reliability.",
        "doi": "10.1109/TED.2021.3055157",
        "displayPublicationTitle": "IEEE Transactions on Electron Devices",
        "displayPublicationDate": "10 February 2021",
        "articleCopyRight": "2021 IEEE",
        "pdfUrl": "/stamp/stamp.jsp?arnumber=9352494",
        "isOpenAccess": false
      };
      </script>
      <main><p>A device reaches high reliability.</p></main>
    </body></html>
    """
    url = "https://ieeexplore.ieee.org/document/9352494"

    assert crawler_mod._is_academic_content(html, url)
    paper = extract_long_html(html, url)

    assert paper.title == "Reliable Skyrmion Logic"
    assert paper.authors == ["Ada Researcher", "Grace Scientist"]
    assert paper.abstract == "A device reaches high reliability."
    assert paper.doi == "10.1109/TED.2021.3055157"
    assert paper.journal == "IEEE Transactions on Electron Devices"
    assert paper.publication_date == "10 February 2021"
    assert paper.license == "2021 IEEE"
    assert paper.pdf_url == (
        "https://ieeexplore.ieee.org/stamp/stamp.jsp?arnumber=9352494"
    )
    assert paper.word_count == 5
    assert paper.sections == []
    assert academic_pdf_candidates(html, url) == []


def test_unsegmented_html_retains_full_text_in_markdown() -> None:
    html = """
    <html><head><meta name="citation_title" content="Short Communication"></head>
    <body><main>
      <p>This communication intentionally has no conventional section headings.</p>
      <p>Its second paragraph must not disappear from the Markdown response.</p>
    </main></body></html>
    """

    paper = extract_long_html(html)
    markdown = paper.to_markdown()

    assert "This communication intentionally has no conventional section headings." in markdown
    assert "Its second paragraph must not disappear" in markdown


@pytest.mark.anyio
async def test_crawler_prefers_pmc_full_text_html_without_downloading_pdf(
    monkeypatch,
) -> None:
    body = " ".join(["full-text"] * 500)
    html = f"""
    <html><head>
      <meta name="citation_title" content="PMC Full Text">
      <meta name="citation_author" content="Reliable Author">
      <meta name="citation_doi" content="10.1234/pmc.full">
      <meta name="citation_pmcid" content="PMC12345">
      <meta name="citation_pdf_url" content="/articles/PMC12345/pdf/article.pdf">
    </head><body><main><article>
      <h1>PMC Full Text</h1>
      <h2>1 Introduction</h2><p>{body}</p>
      <h2>2 Methods</h2><p>Methods remain available in semantic HTML.</p>
    </article></main></body></html>
    """
    calls: list[str] = []

    async def fetch(url, js_render=False, wait_for_selector=None):
        calls.append(url)
        return FetchResult(
            html=html,
            status_code=200,
            content_type="text/html",
            final_url=url,
        )

    monkeypatch.setattr(crawler_mod.fetcher_module, "fetch_url", fetch)
    monkeypatch.setattr(crawler_mod, "_crawl_semaphore", None)

    url = "https://pmc.ncbi.nlm.nih.gov/articles/PMC12345/"
    result = await crawler_mod._crawl_uncached(
        url=url,
        decide_js=False,
        auto_render=False,
        wait_for_selector=None,
        word_count_threshold=10,
        extraction_profile="balanced",
    )

    assert result.error is None
    assert calls == [url]
    assert result.metadata is not None
    assert result.metadata.extraction_strategy == "academic-html"
    assert result.metadata.authors == ["Reliable Author"]
    assert result.metadata.doi == "10.1234/pmc.full"
    assert result.metadata.pmcid == "PMC12345"
    assert result.metadata.canonical_url == url


@pytest.mark.anyio
async def test_crawler_merges_arxiv_landing_metadata_with_pdf_body(
    monkeypatch,
) -> None:
    abstract = " ".join(["structured abstract"] * 60)
    landing_url = "https://export.arxiv.org/abs/1706.03762v7?context=cs#history"
    canonical_url = "https://arxiv.org/abs/1706.03762v7"
    pdf_url = "https://arxiv.org/pdf/1706.03762v7"
    html = f"""
    <html lang="en"><head>
      <meta name="citation_title" content="Authoritative Landing Title">
      <meta name="citation_author" content="Ada Researcher">
      <meta name="citation_author" content="Grace Scientist">
      <meta name="citation_abstract" content="{abstract}">
      <meta name="citation_arxiv_id" content="1706.03762v7">
      <meta name="citation_pdf_url" content="{pdf_url}">
      <link rel="canonical" href="{canonical_url}">
    </head><body><main><blockquote class="abstract">{abstract}</blockquote></main></body></html>
    """
    fetch_calls: list[str] = []

    async def fetch(url, js_render=False, wait_for_selector=None):
        fetch_calls.append(url)
        if url == landing_url:
            return FetchResult(
                html=html,
                status_code=200,
                content_type="text/html",
                final_url=landing_url,
            )
        assert url == pdf_url
        return FetchResult(
            status_code=200,
            content_type="application/pdf",
            raw_bytes=b"%PDF-real-enough-for-the-mock",
            final_url=pdf_url,
        )

    def extract_pdf(contents, url):
        full_text = " ".join(["paper body"] * 800)
        return AcademicPaper(
            title="Inferior PDF Metadata",
            full_text=full_text,
            word_count=len(full_text.split()),
            sections=[Section(heading="Body", content=full_text)],
        )

    monkeypatch.setattr(crawler_mod.fetcher_module, "fetch_url", fetch)
    monkeypatch.setattr(crawler_mod.academic_module, "extract_pdf", extract_pdf)
    monkeypatch.setattr(crawler_mod, "_crawl_semaphore", None)

    result = await crawler_mod._crawl_uncached(
        url=landing_url,
        decide_js=False,
        auto_render=False,
        wait_for_selector=None,
        word_count_threshold=10,
        extraction_profile="balanced",
    )

    assert result.error is None
    assert fetch_calls == [landing_url, pdf_url]
    assert result.metadata is not None
    assert result.metadata.extraction_strategy == "academic-html+pdf"
    assert result.metadata.title == "Authoritative Landing Title"
    assert result.metadata.authors == ["Ada Researcher", "Grace Scientist"]
    assert result.metadata.arxiv_id == "1706.03762v7"
    assert result.metadata.canonical_url == canonical_url
    assert result.metadata.source_url == canonical_url
    assert result.metadata.word_count == crawler_mod._count_output_words(result.markdown)
    assert result.metadata.word_count > 1600
    assert result.links is not None and pdf_url in result.links
    assert "paper body" in result.markdown


@pytest.mark.anyio
async def test_pdf_candidate_parse_failure_falls_back_to_landing(
    monkeypatch,
) -> None:
    abstract = " ".join(["useful abstract"] * 50)
    landing_url = "https://arxiv.org/abs/2401.12345"
    pdf_url = "https://arxiv.org/pdf/2401.12345"
    html = f"""
    <html><head>
      <meta name="citation_title" content="Fallback Paper">
      <meta name="citation_author" content="Safe Author">
      <meta name="citation_abstract" content="{abstract}">
      <meta name="citation_pdf_url" content="{pdf_url}">
    </head><body><blockquote class="abstract">{abstract}</blockquote></body></html>
    """

    async def fetch(url, js_render=False, wait_for_selector=None):
        if url == landing_url:
            return FetchResult(
                html=html,
                status_code=200,
                content_type="text/html",
                final_url=url,
            )
        return FetchResult(
            status_code=200,
            content_type="application/pdf",
            raw_bytes=b"broken-pdf",
            final_url=url,
        )

    def broken_pdf(contents, url):
        raise ValueError("malformed PDF")

    monkeypatch.setattr(crawler_mod.fetcher_module, "fetch_url", fetch)
    monkeypatch.setattr(crawler_mod.academic_module, "extract_pdf", broken_pdf)
    monkeypatch.setattr(crawler_mod, "_crawl_semaphore", None)

    result = await crawler_mod._crawl_uncached(
        url=landing_url,
        decide_js=False,
        auto_render=False,
        wait_for_selector=None,
        word_count_threshold=10,
        extraction_profile="balanced",
    )

    assert result.error is None
    assert result.metadata is not None
    assert result.metadata.extraction_strategy == "academic-landing"
    assert result.metadata.title == "Fallback Paper"
    assert abstract in result.markdown


def test_long_html_emits_nested_semantic_text_once() -> None:
    html = """
    <html>
      <head>
        <meta name="citation_title" content="Nested paper">
      </head>
      <body>
        <article>
          <h1>Nested paper</h1>
          <section>
            <blockquote>
              <p>A quoted result with inline <em>emphasis</em>.</p>
            </blockquote>
            <ul>
              <li><p>First experimental observation.</p></li>
              <li><p>Second experimental observation.</p></li>
            </ul>
            <table>
              <tr><th><p>Metric</p></th><th><p>Value</p></th></tr>
              <tr><td><p>F1</p></td><td><p>0.97</p></td></tr>
            </table>
          </section>
        </article>
      </body>
    </html>
    """

    paper = extract_long_html(html)

    assert paper.full_text.count("A quoted result with inline emphasis.") == 1
    assert paper.full_text.count("First experimental observation.") == 1
    assert paper.full_text.count("Second experimental observation.") == 1
    assert paper.full_text.count("Metric") == 1
    assert paper.full_text.count("0.97") == 1


def test_long_html_removes_non_content_subtrees() -> None:
    html = """
    <html><body>
      <header>Global navigation</header>
      <main><p>The actual research result has enough useful words.</p></main>
      <script>secret_tracking_payload()</script>
      <footer>Terms and privacy</footer>
    </body></html>
    """

    paper = extract_long_html(html)

    assert "actual research result" in paper.full_text
    assert "Global navigation" not in paper.full_text
    assert "secret_tracking_payload" not in paper.full_text
    assert "Terms and privacy" not in paper.full_text


def test_pdf_text_decode_uses_remaining_budget_and_stops_before_next_page(
    monkeypatch,
) -> None:
    import pypdfium2 as pdfium

    range_calls: list[tuple[int, int, int]] = []
    accessed_pages: list[int] = []
    closed_pages: list[int] = []
    closed_text_pages: list[int] = []
    documents = []
    page_text = ("abc", "defgh", "must-not-be-decoded")

    class FakeTextPage:
        def __init__(self, page_number: int) -> None:
            self.page_number = page_number

        def count_chars(self) -> int:
            return len(page_text[self.page_number])

        def get_text_range(self, index=0, count=-1):
            range_calls.append((self.page_number, index, count))
            return page_text[self.page_number][index : index + count]

        def close(self) -> None:
            closed_text_pages.append(self.page_number)

    class FakePage:
        def __init__(self, page_number: int) -> None:
            self.page_number = page_number

        def get_textpage(self):
            return FakeTextPage(self.page_number)

        def close(self) -> None:
            closed_pages.append(self.page_number)

    class FakeDocument:
        def __init__(self, _contents: bytes) -> None:
            self.closed = False
            documents.append(self)

        def __len__(self) -> int:
            return len(page_text)

        def __getitem__(self, page_number: int):
            accessed_pages.append(page_number)
            return FakePage(page_number)

        def get_metadata_dict(self):
            return {}

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(pdfium, "PdfDocument", FakeDocument)
    monkeypatch.setattr(academic_module, "_MAX_ACADEMIC_TEXT_CHARS", 6)

    paper = academic_module.extract_pdf(b"%PDF-test")

    assert paper.full_text == "abc\nde"
    assert len(paper.full_text) == 6
    assert range_calls == [(0, 0, 3), (1, 0, 2)]
    assert accessed_pages == [0, 1]
    assert closed_text_pages == [0, 1]
    assert closed_pages == [0, 1]
    assert documents[0].closed is True
    assert paper.truncated is True
    assert paper.truncation_reason == "PDF text limit (6 characters)"


def test_arxiv_pdf_rejects_publisher_boilerplate_metadata(
    monkeypatch,
) -> None:
    import pypdfium2 as pdfium

    first_page = "\n".join(
        [
            "Attention Is All You Need",
            "Ashish Vaswani, Noam Shazeer, Niki Parmar",
            "Google Brain",
            "Abstract",
            "We propose a reliable attention architecture for sequence modelling.",
            "1 Introduction",
            "Recurrent neural networks were previously the dominant approach.",
        ]
    )

    class FakeTextPage:
        def count_chars(self) -> int:
            return len(first_page)

        def get_text_range(self, index=0, count=-1):
            return first_page[index : index + count]

        def close(self) -> None:
            pass

    class FakePage:
        def get_textpage(self):
            return FakeTextPage()

        def close(self) -> None:
            pass

    class FakeDocument:
        def __init__(self, _contents: bytes) -> None:
            pass

        def __len__(self) -> int:
            return 1

        def __getitem__(self, _page_number: int):
            return FakePage()

        def get_metadata_dict(self):
            return {
                "Title": (
                    "Provided proper attribution is provided, Google hereby "
                    "grants permission to"
                ),
                "Author": "Google Brain",
            }

        def close(self) -> None:
            pass

    monkeypatch.setattr(pdfium, "PdfDocument", FakeDocument)

    paper = academic_module.extract_pdf(
        b"%PDF-test",
        "https://arxiv.org/pdf/1706.03762",
    )

    assert paper.title == "Attention Is All You Need"
    assert paper.authors == ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"]
    assert paper.arxiv_id == "1706.03762"
    assert paper.canonical_url == "https://arxiv.org/abs/1706.03762"
    assert "Provided proper attribution" not in paper.to_markdown()
    assert paper.authors != ["Google Brain"]


def test_pdf_page_limit_and_decode_failure_are_reported(
    monkeypatch,
) -> None:
    import pypdfium2 as pdfium

    class FakeTextPage:
        def count_chars(self) -> int:
            return len("surviving page")

        def get_text_range(self, index=0, count=-1):
            return "surviving page"[index : index + count]

        def close(self) -> None:
            pass

    class FakePage:
        def __init__(self, page_number: int) -> None:
            self.page_number = page_number

        def get_textpage(self):
            if self.page_number == 0:
                raise ValueError("malformed text layer")
            return FakeTextPage()

        def close(self) -> None:
            pass

    class FakeDocument:
        def __init__(self, _contents: bytes) -> None:
            pass

        def __len__(self) -> int:
            return 201

        def __getitem__(self, page_number: int):
            return FakePage(page_number)

        def get_metadata_dict(self):
            return {"Title": "A Recoverable PDF"}

        def close(self) -> None:
            pass

    monkeypatch.setattr(pdfium, "PdfDocument", FakeDocument)

    paper = academic_module.extract_pdf(b"%PDF-test")

    assert paper.word_count == 398
    assert paper.truncated is True
    assert paper.truncation_reason == (
        "PDF page limit (200 pages); PDF page text decode failure"
    )


def test_section_segmentation_assigns_each_section_content_once(monkeypatch) -> None:
    class TrackingSection:
        def __init__(
            self,
            heading: str = "",
            level: int = 1,
            content: str = "",
        ) -> None:
            self.heading = heading
            self.level = level
            self._content = content
            self.content_assignments = 0

        @property
        def content(self) -> str:
            return self._content

        @content.setter
        def content(self, value: str) -> None:
            self.content_assignments += 1
            if self.content_assignments > 1:
                raise AssertionError("section content was repeatedly concatenated")
            self._content = value

    monkeypatch.setattr(academic_module, "Section", TrackingSection)

    sections = academic_module._segment_sections(
        "Preface one\nPreface two\n1 Introduction\nBody one\nBody two\nReferences\nIgnored"
    )

    assert [section.heading for section in sections] == ["Body", "1 Introduction"]
    assert [section.content for section in sections] == [
        "Preface one\nPreface two\n",
        "Body one\nBody two\n",
    ]
    assert [section.content_assignments for section in sections] == [1, 1]


def test_appendix_after_references_is_retained_without_reference_duplication() -> None:
    text = "\n".join(
        [
            "A Reliable Paper",
            "Abstract",
            "A concise summary.",
            "1 Introduction",
            "The main body result.",
            "References",
            "[1] A complete citation. Article CAS PubMed Google Scholar",
            "Google Scholar",
            "Appendix A Additional Experiments",
            "The appendix contains essential ablation details.",
        ]
    )

    sections = academic_module._segment_sections(
        text,
        title="A Reliable Paper",
        abstract="A concise summary.",
    )
    references = academic_module._extract_references(text)

    assert [section.heading for section in sections] == [
        "1 Introduction",
        "Appendix A Additional Experiments",
    ]
    assert "essential ablation details" in sections[-1].content
    assert references == "[1] A complete citation."
    assert "Appendix" not in references
    assert "Google Scholar" not in references


def test_reference_selection_ignores_diagram_labels_before_real_bibliography() -> None:
    text = "\n".join(
        [
            "1 Introduction",
            "The scientific body starts here.",
            "Reference",
            "Model",
            "Reward",
            "Reference Models",
            "Figure 3 demonstrates the training architecture.",
            "2 Results",
            "The main experimental result must remain in the body.",
            "References",
            "A. Author. Reliable systems. Journal 12, 1-9 (2024).",
            "B. Author. Reproducible extraction. https://doi.org/10.1000/example",
            "Appendix A Additional Results",
            "The appendix remains available.",
        ]
    )

    sections = academic_module._segment_sections(text)
    references = academic_module._extract_references(text)
    markdown = "\n".join(section.to_markdown() for section in sections)

    assert "The main experimental result must remain in the body." in markdown
    assert "The appendix remains available." in markdown
    assert references.startswith("A. Author. Reliable systems.")
    assert "Figure 3" not in references


def test_section_segmentation_rejects_keyword_prefix_sentences() -> None:
    sections = academic_module._segment_sections(
        "\n".join(
            [
                "1 Introduction",
                "architectures from the literature remain relevant.",
                "problems, the model exhibits a tendency to verify answers.",
                "results to the base model are reported here.",
                "resulting in a training batch size of 512 per step.",
                "Supplementary E, including comparisons, is discussed later",
                "2 Results",
                "The measured result is reliable.",
            ]
        )
    )

    assert [section.heading for section in sections] == [
        "1 Introduction",
        "2 Results",
    ]
    assert "architectures from the literature" in sections[0].content
    assert "resulting in a training batch size" in sections[0].content


def test_unheaded_body_after_structured_abstract_is_not_dropped() -> None:
    sections = academic_module._segment_sections(
        "\n".join(
            [
                "Abstract",
                "A concise structured abstract.",
                "This unheaded paragraph begins the actual article body.",
                "It must survive even when there is no Introduction heading.",
                "References",
                "A. Author. A source (2024).",
            ]
        ),
        abstract="A concise structured abstract.",
    )

    assert len(sections) == 1
    assert "This unheaded paragraph begins" in sections[0].content
    assert "A concise structured abstract" not in sections[0].content


def test_reference_controls_and_post_reference_chrome_are_removed() -> None:
    html = """
    <html><head>
      <meta name="citation_title" content="A Clean Reference Paper">
      <meta name="citation_abstract" content="A useful abstract.">
    </head><body><article>
      <h2>Introduction</h2><p>The complete article body.</p>
      <h2>References</h2>
      <ol><li><cite>A. Author. A citation (2024).</cite>
        [<a href="https://doi.org/10.1000/example">DOI</a>]
        [<a href="https://pubmed.ncbi.nlm.nih.gov/1/">PubMed</a>]
        [<a href="https://scholar.google.com/example">Google Scholar</a>]
      </li></ol>
      <p class="c-article-references__links">
        <a href="/article">Article</a><a href="/cas">CAS</a>
      </p>
      <h2>Author information</h2>
      <div class="c-article-authors-search">Search author on: PubMed</div>
      <h3>Publisher's Note</h3><p>Generic publisher boilerplate.</p>
      <h2>Appendix A Details</h2><p>Essential appendix result.</p>
    </article></body></html>
    """

    paper = extract_long_html(
        html,
        "https://www.nature.com/articles/reference-cleanup",
    )
    markdown = paper.to_markdown()

    assert paper.references_raw == "A. Author. A citation (2024)."
    assert "Essential appendix result." in markdown
    assert "Google Scholar" not in markdown
    assert "Search author on" not in markdown
    assert "Generic publisher boilerplate" not in markdown


def test_preprint_markup_abstract_becomes_clean_landing_content() -> None:
    html = """
    <html><head>
      <meta name="citation_title" content="A Markup Preprint">
      <meta name="citation_abstract"
            content="&lt;h3&gt;Background&lt;/h3&gt;&lt;p&gt;Clean text.&lt;/p&gt;">
      <meta name="citation_author" content="Ada Researcher">
    </head><body><article>
      <div class="author-tooltip-find-more">
        <a>Find this author on Google Scholar</a>
      </div>
      <h3>Abstract</h3><p>Background Clean text.</p>
    </article></body></html>
    """

    paper = extract_long_html(
        html,
        "https://www.biorxiv.org/content/10.1101/2024.01.02v1",
    )
    markdown = paper.to_markdown()

    assert paper.abstract == "Background Clean text."
    assert paper.sections == []
    assert "<h3>" not in markdown
    assert "Find this author" not in markdown


def test_reference_cap_uses_boundary_and_explicit_truncation_marker() -> None:
    references = "\n".join(
        f"[{index}] " + ("bounded reference text " * 12) + "."
        for index in range(200)
    )

    extracted = academic_module._extract_references(
        f"Paper body\nReferences\n{references}"
    )

    assert len(extracted) <= 15_000
    assert extracted.endswith(
        "<!-- references truncated at 15000 characters -->"
    )
    before_marker = extracted.rsplit("\n\n", 1)[0]
    assert before_marker.endswith(".")


def test_reference_cap_sets_paper_truncation_metadata() -> None:
    references = "".join(
        f"<p>[{index}] " + ("bounded reference text " * 12) + ".</p>"
        for index in range(200)
    )
    html = f"""
    <html><body><article>
      <h1>A Bounded Reference Paper</h1>
      <h2>1 Introduction</h2>
      <p>The article body remains complete.</p>
      <h2>References</h2>
      {references}
    </article></body></html>
    """

    paper = extract_long_html(html, "https://www.nature.com/articles/example")

    assert paper.truncated is True
    assert paper.truncation_reason == "references limit (15000 characters)"
    assert "<!-- references truncated at 15000 characters -->" in paper.to_markdown()


def test_html_text_cap_sets_paper_truncation_metadata(monkeypatch) -> None:
    monkeypatch.setattr(academic_module, "_MAX_ACADEMIC_TEXT_CHARS", 50)
    html = "<html><body><main><p>" + ("substantive " * 30) + "</p></main></body></html>"

    paper = extract_long_html(html, "https://www.science.org/doi/10.1000/example")

    assert len(paper.full_text) == 50
    assert paper.truncated is True
    assert paper.truncation_reason == "HTML text limit (50 characters)"
