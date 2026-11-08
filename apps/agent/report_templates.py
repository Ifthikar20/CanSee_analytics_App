"""Report templates: the fixed, code-defined set a user can pick for a
report (GET agent/report-templates/).

A template does not render anything by itself. Picking one when sending a
message (``template`` on the send body) adds a system note to that turn
with the template's outline (prompts.context.report_template), records
the key on the reply (``research["template"]``), and saving the reply as
a report copies the key and a structured header into the version's data
(services.reports). Old reports without a template stay unlabelled.

The outline text lives here, not in the orchestrator's system prompt, so
changing a template never changes the cached prompt prefix; chats pinned
to an older snapshot still get the outline through the note.
"""
from __future__ import annotations

from dataclasses import dataclass

# Data windows of the read tools (search_gap.services.overview.WINDOW_DAYS
# and measured.perception), restated here so the catalog needs no imports.
OVERVIEW_WINDOW_DAYS = 120
PERCEPTION_WINDOW_DAYS = 90
HISTORY_WINDOW_DAYS = 90

# How a template uses Google search and the AI engines.
REQUIRED, OPTIONAL, NOT_USED, SAVED = "required", "optional", "not_used", "saved"

# Header fields every report header carries (services.reports.report_header).
BASE_HEADER = ("template", "project", "captured_at", "data", "engines", "locations")


@dataclass(frozen=True)
class Section:
    title: str
    # What the section shows, in one plain sentence.
    shows: str
    # Where its data comes from (tool or capture), in plain words.
    source: str
    # "prompt": one section per prompt, titled with the prompt.
    repeat: str | None = None


@dataclass(frozen=True)
class ReportTemplate:
    key: str
    name: str
    purpose: str
    sections: tuple[Section, ...]
    # How to gather the data and what to avoid: lines of the turn's note.
    rules: tuple[str, ...]
    # Header fields beyond BASE_HEADER.
    extra_header: tuple[str, ...] = ()
    # Tools the chat's snapshot must offer for the template to work.
    required_tools: tuple[str, ...] = ()
    # Google search and the AI engines: REQUIRED, OPTIONAL, NOT_USED or
    # SAVED (reads data already measured, runs no new check).
    google: str = SAVED
    engines: str = SAVED
    # Whether the writer may run the research agents (research_swarm).
    research_agents: bool = False
    # Whether the template turns on Sentiment and Fact check data.
    needs_insights: bool = False
    # The results-table sheets the report's "Research agents" appendix keeps.
    sheets: tuple[str, ...] = ("prompts", "gaps")
    # Shown on the card: what the template asks of the workspace.
    requirement_note: str = ""
    # Runs new Google searches or engine questions (costs prompt runs).
    runs_checks: bool = False
    # The tools the rules name (a test keeps them real tool names).
    tools: tuple[str, ...] = ()

    @property
    def header_fields(self) -> tuple[str, ...]:
        return (*BASE_HEADER, *self.extra_header)


METHOD = Section(
    "Method",
    "When the data was captured, the data window, the location, the engines asked and the "
    "engines that are not configured.",
    "The tool results in this reply",
)

_TEMPLATES = (
    ReportTemplate(
        key="executive_summary",
        name="Executive summary",
        purpose=(
            "A one-page status for leadership: the headline numbers, the biggest gaps and the "
            "top fixes, from data already measured."
        ),
        sections=(
            Section("Key numbers",
                    "Tracked prompts, AI visibility, average Google position and prompts not "
                    "in Google's results.",
                    f"Project overview, last {OVERVIEW_WINDOW_DAYS} days"),
            Section("Gap mix",
                    "How many prompts are AI ahead, Google ahead, strong in both, invisible in "
                    "both, even or not checked.",
                    "Project overview"),
            Section("Who AI recommends",
                    "The brands the AI engines name most often.",
                    "Project overview"),
            Section("Biggest gaps",
                    "The three prompts with the largest gap between Google and the AI engines.",
                    "Project overview and the prompt gap of each"),
            Section("Top fixes",
                    "Up to five fixes, each naming the page to change or create.",
                    "Recommended fixes of the prompt gaps"),
            METHOD,
        ),
        rules=(
            "Call get_project_overview, then get_prompt_gap for the three prompts with the largest "
            "gap.",
            "Do not call google_search, check_ai_engines or research_swarm: this report uses data "
            "already measured.",
            "Write the key numbers as a short list, not prose.",
        ),
        tools=("get_project_overview", "get_prompt_gap"),
        requirement_note="Uses data already measured. Runs no new Google searches or AI engine "
                         "questions.",
    ),
    ReportTemplate(
        key="google_ai_gap",
        name="Google vs AI gap",
        purpose=(
            "Prompt by prompt, where Google ranks you compared with what the AI engines say, "
            "and the fixes for each gap."
        ),
        sections=(
            Section("Summary", "Three sentences on where the brand stands.",
                    "The research in this reply"),
            Section("Results",
                    "One row per prompt: the Google position, each engine's result and the gap.",
                    "Research agents' results table, or Google search and engine checks"),
            Section("Prompt",
                    "What Google shows, what each engine says and which brands it lists above "
                    "the brand, what competitor pages do that the brand's page does not, the "
                    "cited sources, the gap and 2 to 4 fixes.",
                    "Google results, engine answers, page checks and the prompt gap",
                    repeat="prompt"),
            Section("Content gaps",
                    "What the brand's page lacks compared with the competitor pages.",
                    "Research agents' content gap analysis"),
            METHOD,
        ),
        rules=(
            "Research 2 to 4 tracked prompts with research_swarm and its default plan (see "
            "\"Research agents\"); without research_swarm, follow the research workflow.",
            "When the research agents ran, the app shows their results table above the reply and "
            "in the saved report: under Results, point to it in one sentence instead of repeating "
            "it. Otherwise write the Results table with the columns Prompt | Google | ChatGPT | "
            "Claude | Gemini | Grok | Perplexity | Gap.",
            "Without the research agents, write Content gaps as \"Not measured\".",
        ),
        tools=("research_swarm", "list_tracked_prompts", "google_search", "check_ai_engines",
               "get_prompt_gap", "inspect_page"),
        google=OPTIONAL,
        engines=OPTIONAL,
        research_agents=True,
        runs_checks=True,
        requirement_note="Runs new Google searches and AI engine questions for 2 to 4 prompts. "
                         "Engines that are not set up are reported as not configured.",
    ),
    ReportTemplate(
        key="competitor_comparison",
        name="Competitor comparison",
        purpose="You against one to three tracked competitors, prompt by prompt.",
        sections=(
            Section("Share of voice",
                    "How often the AI engines name the brand and each competitor.",
                    "Project overview"),
            Section("Where competitors are ahead",
                    "Per prompt and engine, the competitors listed above the brand, and their "
                    "Google positions.",
                    "Prompt gaps, or the research agents' engine answers"),
            Section("What their pages do",
                    "What the competitor pages do that the brand's page does not.",
                    "Competitor pages the research agents or a page check read"),
            Section("Sources that favour competitors",
                    "Competitor-owned pages the engines cite.",
                    f"Cited sources, last {OVERVIEW_WINDOW_DAYS} days"),
            Section("Fixes",
                    "2 to 5 fixes, each naming the page to change or create.",
                    "The sections above"),
            METHOD,
        ),
        rules=(
            "Compare with the competitors the user named (one to three), else the project's "
            "tracked competitors.",
            "Call get_project_overview and get_cited_sources. For 2 to 3 tracked prompts, use "
            "research_swarm with those competitors' domains in competitors; without "
            "research_swarm, call get_prompt_gap per prompt and inspect_page for the strongest "
            "competitor result.",
            "End Fixes by offering a comparison page (run_agent comparison_page) for the "
            "strongest competitor.",
        ),
        extra_header=("competitors",),
        tools=("get_project_overview", "get_cited_sources", "research_swarm", "get_prompt_gap",
               "inspect_page"),
        google=OPTIONAL,
        engines=OPTIONAL,
        research_agents=True,
        runs_checks=True,
        requirement_note="Uses your tracked competitors. Runs new Google searches and AI engine "
                         "questions for 2 to 3 prompts.",
    ),
    ReportTemplate(
        key="prompt_deep_dive",
        name="Prompt deep dive",
        purpose="One tracked prompt, end to end: Google, every engine, the sources and the fixes.",
        sections=(
            Section("Verdict and gap", "The gap between Google and the AI engines, and its label.",
                    "Prompt gap"),
            Section("Google results and AI Overview",
                    "The top results, the brand's position and whether the AI Overview names or "
                    "cites the brand.",
                    "Google search, 2 pages"),
            Section("What each engine said",
                    "Whether each engine names the brand, at what rank, with a short quote.",
                    "The latest answer of each engine"),
            Section("Sources cited", "The domains the engines cite for this prompt.",
                    f"Cited sources for this prompt, last {OVERVIEW_WINDOW_DAYS} days"),
            Section("On-page check",
                    "The brand's best-ranking page compared with the strongest competitor page.",
                    "Page checks"),
            Section("Fixes", "2 to 4 fixes, each naming the page to change or create.",
                    "The sections above"),
            METHOD,
        ),
        rules=(
            "Cover one tracked prompt: the one the user named; if none is named, ask which.",
            "Call get_prompt_gap, google_search with its prompt_id and pages=2, get_ai_answers, "
            "get_cited_sources with its prompt_id, and inspect_page for the brand's best-ranking "
            "result and the strongest competitor result (URLs from the Google results only).",
            "Do not call research_swarm. Ask the engines again (check_ai_engines) only when the "
            "user asks for fresh answers.",
            "Quote engines briefly (one sentence each) and mark quotes as the engine's words.",
        ),
        extra_header=("prompts",),
        tools=("get_prompt_gap", "google_search", "get_ai_answers", "get_cited_sources",
               "inspect_page"),
        google=REQUIRED,
        engines=SAVED,
        runs_checks=True,
        requirement_note="Searches Google for one prompt (2 results pages) and reads the latest "
                         "saved engine answers.",
    ),
    ReportTemplate(
        key="sources_citations",
        name="Sources and citations",
        purpose="Which domains the AI engines cite, and where the brand should get mentioned.",
        sections=(
            Section("Totals",
                    "Citations in the window and how many point to the brand's own site.",
                    f"Cited sources, last {OVERVIEW_WINDOW_DAYS} days"),
            Section("Top sources",
                    "Each source's domain, citations, the engines citing it, and whether it is "
                    "the brand's or a competitor's.",
                    "Cited sources"),
            Section("Sources to target",
                    "Third-party and competitor-owned sources the engines cite where the brand "
                    "is absent.",
                    "Cited sources"),
            Section("Outreach list", "Example pages to pitch, per target source.",
                    "Example URLs of the cited sources"),
            METHOD,
        ),
        rules=(
            "Call get_cited_sources without prompt_id, then with prompt_id for up to three "
            "prompts the user named.",
            "Do not call google_search, check_ai_engines or research_swarm: this report uses "
            "citations already measured.",
            "List only URLs from the tool results; never build one.",
        ),
        tools=("get_cited_sources", "list_tracked_prompts"),
        sheets=("prompts", "gaps", "sources"),
        requirement_note="Uses citations already measured. Runs no new Google searches or AI "
                         "engine questions.",
    ),
    ReportTemplate(
        key="brand_accuracy",
        name="Brand accuracy",
        purpose=(
            "How the AI engines describe the brand, and which of their claims the brand's own "
            "evidence contradicts."
        ),
        sections=(
            Section("Coverage", "Answers that name the brand out of the answers analyzed.",
                    f"Sentiment, last {PERCEPTION_WINDOW_DAYS} days"),
            Section("How the engines describe the brand",
                    "Each theme with its tone, how many answers, the engines and one quote.",
                    f"Sentiment, last {PERCEPTION_WINDOW_DAYS} days"),
            Section("Contradicted claims", "Each claim the evidence contradicts, with the evidence.",
                    f"Fact check, last {PERCEPTION_WINDOW_DAYS} days"),
            Section("Corrections to request",
                    "The correction to ask for, per contradicted claim.",
                    "Fact check and the brand context"),
            METHOD,
        ),
        rules=(
            "Call get_brand_perception, then get_brand_context to check claims against the brand "
            "context.",
            "Do not call google_search, check_ai_engines or research_swarm.",
            "Report only perception and Fact check findings; leave out brand security alerts.",
            "Under Corrections to request, offer a correction request (run_agent pr_writer, "
            "format correction_request, with the claim's claim_key) for each contradicted claim.",
            "If get_brand_perception says the data is not available, say so plainly and write "
            "each section as \"Not measured\".",
        ),
        tools=("get_brand_perception", "get_brand_context"),
        required_tools=("get_brand_perception",),
        needs_insights=True,
        requirement_note="Uses the Sentiment and Fact check analysis of saved engine answers. "
                         "Runs no new checks.",
    ),
    ReportTemplate(
        key="change_log",
        name="Change log since the last run",
        purpose="What moved since the previous check of each prompt, and what to do next.",
        sections=(
            Section("What changed",
                    "Prompts and engines whose result moved since the previous check: gains "
                    "first, then losses.",
                    f"Visibility history, last {HISTORY_WINDOW_DAYS} days"),
            Section("Unchanged", "How many prompt and engine results did not change.",
                    "Visibility history"),
            Section("Visibility trend",
                    "Weekly AI visibility, overall and per engine.",
                    f"Visibility history, last {HISTORY_WINDOW_DAYS} days"),
            Section("Next actions", "2 to 4 actions, each naming the page to change or create.",
                    "The sections above"),
            METHOD,
        ),
        rules=(
            "Call get_visibility_history. Take What changed and Unchanged from its changes, and "
            "the trend from its weekly series; chart only weeks with answers (show_chart, line).",
            "\"The last run\" is each prompt's previous engine check. When this chat already has "
            "a research_swarm result and the user asks for fresh data, run it again with rerun_of "
            "and nothing else; its results table marks every changed cell.",
            "In Method, name the dates compared.",
        ),
        extra_header=("compared",),
        tools=("get_visibility_history", "show_chart", "research_swarm"),
        required_tools=("get_visibility_history",),
        requirement_note="Compares each prompt's latest engine answer with the one before it. "
                         "Runs no new checks.",
    ),
)

TEMPLATES: dict[str, ReportTemplate] = {t.key: t for t in _TEMPLATES}
KEYS: tuple[str, ...] = tuple(TEMPLATES)


def get(key) -> ReportTemplate | None:
    return TEMPLATES.get(key) if isinstance(key, str) else None


def outline_lines(template: ReportTemplate) -> list[str]:
    """The numbered section list of the turn's note."""
    lines = []
    for number, section in enumerate(template.sections, start=1):
        title = ("One section per prompt, titled with the prompt"
                 if section.repeat == "prompt" else section.title)
        lines.append(f"{number}. {title}: {section.shows}")
    return lines
