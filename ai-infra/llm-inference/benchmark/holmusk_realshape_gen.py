"""Generate REAL-SHAPE synthetic psychiatry notes for the p6 benchmark.

Why: every p6 number so far was measured on SHORT notes (~250 in / ~110 out).
Holmusk's real phase-1 workload (GPU info-Holmusk.xlsx) is ~3,000 input +
~1,000 output tokens per note (36B in + 12B out over 12M notes), with only
~1% of the prompt cacheable. This generator produces notes that match that
shape so the benchmark measures what the customer will actually run.

Design constraints (all deliberate):
- Token counts measured with the REAL MedGemma tokenizer (hf tokenizer.json),
  target 3,000 +/- 250 input tokens per note.
- Each note begins with a UNIQUE encounter line, so vLLM's prefix cache can
  match only the shared system prompt (~= the customer's "1% cacheable").
  Content is also pool-shuffled per note; prefix matching is position-0
  anchored, so nothing past the first divergent token can hit anyway.
- 8-12 medications per note (current + past + PRN + somatic comorbidity meds)
  so the medication-extraction JSON output naturally runs ~800-1,200 tokens,
  matching their ~1,000-token output shape.
- Pool size >= the largest benchmark tier's request count, so LLMeter never
  cycles a payload within a tier (a repeated note would be a full-prompt
  cache hit and inflate throughput).
- Fully synthetic vocabulary — NO real data anywhere near this file. Output
  goes to ~/holmusk-bench-local/ (never a repo, never OneDrive-critical-path).

Usage:
  ../.venv/bin/python holmusk_realshape_gen.py [--n 40000] [--target-tokens 2900]

Token math: their sheet says 36B input tokens / 12M notes = 3,000 per REQUEST.
A request = system prompt (84 tok) + chat template (~20) + note, so the note
itself targets ~2,900.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

OUT_DIR = Path.home() / "holmusk-bench-local" / "real-shape-notes"

# Their exact prompt, verbatim from GPU info-Holmusk.xlsx cell B52.
SYSTEM_PROMPT_REAL = (
    "You are a clinical NLP assistant that extracts medication information from "
    "psychiatric notes.Given a clinical text, extract all mentioned medications "
    "and return ONLY a valid JSON object conforming to this schema: "
    "{drug:string, dosage:string, route:string, frequency:string, duration:string, "
    "temporal_status:string, changed_drug_status:string, change_reasons: list, "
    "side_effects:list}"
)

# --------------------------------------------------------------------------
# Vocabulary pools (fully synthetic)
# --------------------------------------------------------------------------
MEDS: dict[str, dict] = {
    "Sertraline": {"doses": ["50mg", "100mg", "150mg", "200mg"], "freq": ["daily", "every morning"]},
    "Escitalopram": {"doses": ["10mg", "20mg"], "freq": ["daily"]},
    "Fluoxetine": {"doses": ["20mg", "40mg", "60mg"], "freq": ["daily", "every morning"]},
    "Paroxetine": {"doses": ["20mg", "30mg", "40mg"], "freq": ["daily"]},
    "Citalopram": {"doses": ["20mg", "40mg"], "freq": ["daily"]},
    "Venlafaxine XR": {"doses": ["75mg", "150mg", "225mg"], "freq": ["daily", "every morning"]},
    "Duloxetine": {"doses": ["30mg", "60mg", "90mg"], "freq": ["daily"]},
    "Desvenlafaxine": {"doses": ["50mg", "100mg"], "freq": ["daily"]},
    "Bupropion XL": {"doses": ["150mg", "300mg", "450mg"], "freq": ["every morning"]},
    "Mirtazapine": {"doses": ["15mg", "30mg", "45mg"], "freq": ["at bedtime"]},
    "Trazodone": {"doses": ["50mg", "100mg", "150mg"], "freq": ["at bedtime", "as needed for sleep"]},
    "Nortriptyline": {"doses": ["25mg", "50mg", "75mg"], "freq": ["at bedtime"]},
    "Lithium": {"doses": ["300mg", "600mg", "900mg", "1200mg"], "freq": ["daily", "twice daily"]},
    "Lamotrigine": {"doses": ["25mg", "100mg", "200mg"], "freq": ["daily", "twice daily"]},
    "Valproate": {"doses": ["500mg", "1000mg"], "freq": ["twice daily", "at bedtime"]},
    "Oxcarbazepine": {"doses": ["300mg", "600mg"], "freq": ["twice daily"]},
    "Quetiapine": {"doses": ["25mg", "50mg", "100mg", "300mg"], "freq": ["at bedtime", "HS"]},
    "Aripiprazole": {"doses": ["2mg", "5mg", "10mg", "15mg"], "freq": ["daily"]},
    "Risperidone": {"doses": ["0.5mg", "1mg", "2mg", "3mg"], "freq": ["daily", "twice daily"]},
    "Olanzapine": {"doses": ["2.5mg", "5mg", "10mg"], "freq": ["at bedtime"]},
    "Lurasidone": {"doses": ["20mg", "40mg", "80mg"], "freq": ["daily with food"]},
    "Ziprasidone": {"doses": ["40mg", "80mg"], "freq": ["twice daily with food"]},
    "Paliperidone palmitate": {"doses": ["117mg", "156mg"], "freq": ["monthly"], "route": "intramuscular"},
    "Clozapine": {"doses": ["100mg", "200mg", "300mg"], "freq": ["twice daily"]},
    "Lorazepam": {"doses": ["0.5mg", "1mg", "2mg"], "freq": ["as needed", "twice daily as needed"]},
    "Clonazepam": {"doses": ["0.25mg", "0.5mg", "1mg"], "freq": ["twice daily", "at bedtime"]},
    "Alprazolam": {"doses": ["0.25mg", "0.5mg"], "freq": ["as needed"]},
    "Zolpidem": {"doses": ["5mg", "10mg"], "freq": ["at bedtime as needed"]},
    "Eszopiclone": {"doses": ["1mg", "2mg", "3mg"], "freq": ["at bedtime"]},
    "Buspirone": {"doses": ["10mg", "15mg", "30mg"], "freq": ["twice daily", "three times daily"]},
    "Hydroxyzine": {"doses": ["25mg", "50mg"], "freq": ["as needed for anxiety"]},
    "Prazosin": {"doses": ["1mg", "2mg", "5mg"], "freq": ["at bedtime"]},
    "Gabapentin": {"doses": ["300mg", "600mg", "900mg"], "freq": ["three times daily", "at bedtime"]},
    "Propranolol": {"doses": ["10mg", "20mg", "40mg"], "freq": ["twice daily", "as needed"]},
    "Methylphenidate XR": {"doses": ["18mg", "27mg", "36mg", "54mg"], "freq": ["every morning"]},
    "Lisdexamfetamine": {"doses": ["30mg", "50mg", "70mg"], "freq": ["every morning"]},
    "Atomoxetine": {"doses": ["40mg", "80mg"], "freq": ["daily"]},
    "Guanfacine ER": {"doses": ["1mg", "2mg", "3mg"], "freq": ["at bedtime"]},
    "Naltrexone": {"doses": ["50mg"], "freq": ["daily"]},
    "Acamprosate": {"doses": ["666mg"], "freq": ["three times daily"]},
    "Melatonin": {"doses": ["3mg", "5mg", "10mg"], "freq": ["at bedtime"]},
    "Levothyroxine": {"doses": ["50mcg", "75mcg", "100mcg"], "freq": ["every morning"]},
    "Metformin": {"doses": ["500mg", "1000mg"], "freq": ["twice daily with meals"]},
    "Lisinopril": {"doses": ["10mg", "20mg"], "freq": ["daily"]},
    "Atorvastatin": {"doses": ["10mg", "20mg", "40mg"], "freq": ["at bedtime"]},
    "Omeprazole": {"doses": ["20mg", "40mg"], "freq": ["daily before breakfast"]},
    "Vitamin D3": {"doses": ["1000 IU", "2000 IU", "5000 IU"], "freq": ["daily"]},
    "Multivitamin": {"doses": ["one tablet"], "freq": ["daily"]},
}

SIDE_EFFECTS = [
    "nausea", "dry mouth", "daytime sedation", "initial insomnia", "weight gain",
    "fine hand tremor", "akathisia", "dizziness on standing", "morning headache",
    "decreased libido", "constipation", "night sweats", "vivid dreams",
    "reduced appetite", "mild fatigue", "restlessness", "bruxism", "polyuria",
]
CHANGE_REASONS = [
    "persistent side effects", "inadequate therapeutic response", "cost concerns",
    "patient preference", "potential drug-drug interaction", "elevated lab values",
    "planned pregnancy", "excessive daytime sedation", "insurance formulary change",
    "partial response at maximum tolerated dose",
]
DIAGNOSES = [
    "major depressive disorder, recurrent, moderate", "generalized anxiety disorder",
    "bipolar I disorder, most recent episode depressed", "bipolar II disorder",
    "post-traumatic stress disorder", "panic disorder", "social anxiety disorder",
    "persistent depressive disorder", "schizoaffective disorder, bipolar type",
    "obsessive-compulsive disorder", "attention-deficit/hyperactivity disorder",
    "alcohol use disorder, in early remission", "borderline personality disorder",
    "adjustment disorder with mixed anxiety and depressed mood", "insomnia disorder",
]
HPI_SENTENCES = [
    "The patient reports {mood} mood over the past {n} weeks with {sleep} sleep and {app} appetite.",
    "Energy levels are described as {energy}, and concentration at work has been {conc}.",
    "The patient describes intermittent episodes of {sx} lasting {dur}, typically triggered by {trigger}.",
    "Anxiety is rated {n}/10 on average days, rising to {n2}/10 during acute episodes.",
    "The patient endorses {freqword} rumination about {topic}, particularly in the evenings.",
    "Panic symptoms including palpitations, diaphoresis, and a sense of impending doom occur roughly {n} times per month.",
    "Sleep onset latency is approximately {n0} minutes with {n} nocturnal awakenings on a typical night.",
    "The patient reports adherence to the current regimen with {miss} missed doses in the past month.",
    "Appetite has {app2}, with an associated weight {wdir} of {n} pounds since the last visit.",
    "The patient continues to attend {ther} psychotherapy sessions {therfreq} and finds them {therhelp}.",
    "Alcohol intake is {etoh}; the patient denies illicit substance use.",
    "Caffeine intake is approximately {n} cups of coffee daily, which may be contributing to sleep difficulties.",
    "The patient notes that symptoms are {better} on workdays and {worse} on unstructured days.",
    "Interpersonal stressors include ongoing tension with {person}, which the patient links to symptom exacerbation.",
    "Occupational functioning remains {func}, with {n} sick days taken in the past quarter.",
    "The patient describes passive thoughts of hopelessness without active suicidal ideation, plan, or intent.",
    "There have been no emergency department visits or hospitalizations since the previous appointment.",
    "The patient reports practicing sleep hygiene measures with {adh} consistency.",
    "Mood charting shows {n} discrete low-mood days in the past month, an improvement from {n2} previously.",
    "The patient has resumed {hobby}, which they identify as a meaningful behavioral activation target.",
]
MSE_SENTENCES = [
    "Appearance: casually dressed, adequately groomed, appears stated age.",
    "Behavior: cooperative and engaged, with appropriate eye contact throughout the interview.",
    "Motor: no psychomotor agitation or retardation observed; no tremor or abnormal involuntary movements noted on examination.",
    "Speech: normal rate, rhythm, volume, and prosody; no pressured speech.",
    "Mood is reported as '{moodq}'; affect is {affect}, congruent with stated mood, and appropriately reactive.",
    "Thought process: linear, logical, and goal-directed without tangentiality or circumstantiality.",
    "Thought content: no delusions, obsessions, or overvalued ideas elicited; no thought broadcasting or insertion.",
    "Perception: denies auditory, visual, tactile, or olfactory hallucinations; no responding to internal stimuli observed.",
    "Cognition: alert and fully oriented; attention and concentration grossly intact on conversational testing; recent and remote memory intact.",
    "Insight is {insight} and judgment is {judgment}.",
    "The patient denies current suicidal ideation, intent, or plan, and denies homicidal ideation.",
    "No evidence of catatonia; no waxy flexibility, negativism, or echophenomena.",
]
HISTORY_SENTENCES = [
    "Psychiatric history is notable for {n} prior major depressive episodes, the first at age {age}.",
    "There is one remote psychiatric hospitalization approximately {n} years ago for safety concerns; none since.",
    "The patient has trialed multiple antidepressants historically with variable tolerability, detailed in the medication review below.",
    "Family history is significant for {famdx} in a first-degree relative and completed suicide in a second-degree relative.",
    "Medical history includes {meddx1} and {meddx2}, both followed by the primary care physician.",
    "The patient has no known drug allergies; an earlier documented reaction to {alg} was reclassified as intolerance.",
    "Social history: the patient lives with {living}, works as {job}, and identifies {support} as primary supports.",
    "Developmental history is unremarkable; educational attainment is {edu}.",
    "There is no history of seizures, head trauma with loss of consciousness, or other neurological illness.",
    "Tobacco: {tob}. The patient was counseled on cessation resources as applicable.",
    "Legal history is negative; there are no current custody or forensic involvements.",
    "Trauma history was reviewed and is documented separately in the intake assessment; the patient declined to revisit details today.",
]
LAB_SENTENCES = [
    "Most recent lithium level was {lith} mEq/L, within the therapeutic window; renal function and TSH remain normal.",
    "Comprehensive metabolic panel from {mon} was unremarkable apart from a fasting glucose of {glu} mg/dL.",
    "Hemoglobin A1c was {a1c}%, prompting reinforcement of dietary counseling given antipsychotic exposure.",
    "Lipid panel showed LDL of {ldl} mg/dL; repeat testing is scheduled in three months.",
    "Valproate trough level was {vpa} mcg/mL with normal hepatic transaminases and platelet count.",
    "Absolute neutrophil count remains above threshold for continued clozapine dispensing per registry requirements.",
    "TSH was {tsh} mIU/L; thyroid replacement dosing is unchanged.",
    "Vital signs today: blood pressure {bp}, heart rate {hr}, weight {wt} pounds, BMI {bmi}.",
    "ECG from the last visit demonstrated a QTc of {qtc} ms; repeat is planned given ongoing antipsychotic therapy.",
    "Urine toxicology from the previous visit was negative for all tested substances.",
    "PHQ-9 today scored {phq} compared with {phq2} at the prior visit; GAD-7 scored {gad} compared with {gad2}.",
    "Prolactin level was {prl} ng/mL and asymptomatic; monitoring will continue.",
]
PLAN_SENTENCES = [
    "Continue current psychotherapy with emphasis on cognitive restructuring and behavioral activation targets.",
    "Reinforced sleep hygiene: consistent wake time, morning light exposure, and limiting caffeine after noon.",
    "Safety plan reviewed and updated; the patient retains crisis line contacts and agreed to present to the emergency department for acute safety concerns.",
    "Coordination of care: a summary will be sent to the primary care physician with the patient's consent.",
    "Discussed risks, benefits, side effects, and alternatives of all medication changes; the patient verbalized understanding and agreement.",
    "Laboratory orders placed for the monitoring panel described above, to be drawn prior to the next appointment.",
    "The patient was encouraged to continue daily mood charting and to bring the log to the next visit.",
    "Return to clinic in {n} weeks, sooner if symptoms worsen; telehealth follow-up offered as an alternative.",
    "Discussed exercise prescription of {n0} minutes of moderate aerobic activity most days of the week.",
    "Provided psychoeducation regarding relapse warning signs and early intervention strategies.",
    "Referral placed for {ref} to augment current treatment.",
    "Time was spent counseling on medication adherence strategies including a pill organizer and phone reminders.",
]
TELEHEALTH_BLOCK = (
    "The visit was conducted via secure telehealth platform. Identity and physical location "
    "of the patient were confirmed at the start of the session. Consent for telehealth was "
    "obtained and documented, including discussion of its limitations, privacy considerations, "
    "and the patient's right to discontinue and request an in-person appointment at any time. "
    "Audio and video quality were adequate for clinical assessment throughout."
)

MED_STATUS_TEMPLATES = [
    ("current", "continued",
     "{drug} {dose} {route} {freq} — continued without change; the patient reports {tol} tolerability{se}."),
    ("current", "increased",
     "{drug} increased from {dose0} to {dose} {route} {freq} due to {reason}; early response will be reassessed at the next visit{se}."),
    ("current", "decreased",
     "{drug} decreased from {dose0} to {dose} {route} {freq} because of {reason}{se}."),
    ("new", "started",
     "{drug} {dose} {route} {freq} started this visit targeting {target}; counseled on onset expectations and common early effects{se}."),
    ("tapering", "discontinued",
     "{drug} is being tapered from {dose} toward discontinuation over {taper} due to {reason}; withdrawal precautions reviewed{se}."),
    ("past", "discontinued",
     "Historically the patient trialed {drug} (reached {dose} {freq}) for approximately {trialdur}, discontinued due to {reason}."),
    ("past", "switched",
     "{drug} was previously switched to {switchto} after {trialdur} because of {reason}."),
    ("current", "continued",
     "For comorbid medical management the patient remains on {drug} {dose} {route} {freq}, prescribed externally{se}."),
]


def _fill(rng: random.Random, template: str) -> str:
    subs = {
        "mood": rng.choice(["gradually improving", "persistently low", "stable but guarded",
                            "notably brighter", "fluctuating"]),
        "sleep": rng.choice(["fragmented", "consolidated", "delayed-onset", "restorative"]),
        "app": rng.choice(["reduced", "normal", "increased"]),
        "app2": rng.choice(["improved", "diminished", "remained stable"]),
        "energy": rng.choice(["low but improving", "adequate", "markedly reduced", "near baseline"]),
        "conc": rng.choice(["impaired", "mildly reduced", "back to baseline"]),
        "sx": rng.choice(["low mood with tearfulness", "acute anxiety", "irritability",
                          "depersonalization", "racing thoughts"]),
        "dur": rng.choice(["several hours", "most of a day", "twenty to thirty minutes"]),
        "trigger": rng.choice(["work deadlines", "family conflict", "poor sleep the prior night",
                               "financial stressors", "anniversary reactions"]),
        "freqword": rng.choice(["frequent", "occasional", "near-daily"]),
        "topic": rng.choice(["past decisions", "job security", "health worries", "relationships"]),
        "miss": rng.choice(["no", "one or two", "three"]),
        "wdir": rng.choice(["gain", "loss"]),
        "ther": rng.choice(["individual CBT", "supportive", "trauma-focused", "group DBT"]),
        "therfreq": rng.choice(["weekly", "biweekly", "twice monthly"]),
        "therhelp": rng.choice(["helpful", "moderately helpful", "increasingly valuable"]),
        "etoh": rng.choice(["two to three drinks weekly", "rare social use", "none since the last visit"]),
        "better": rng.choice(["better", "more manageable"]),
        "worse": rng.choice(["worse", "more pronounced"]),
        "person": rng.choice(["a sibling", "a coworker", "their partner", "an adult child"]),
        "func": rng.choice(["intact", "mildly impaired", "improving"]),
        "adh": rng.choice(["good", "partial", "inconsistent"]),
        "hobby": rng.choice(["regular walking", "a painting class", "weekend cycling", "gardening"]),
        "moodq": rng.choice(["okay", "better", "flat", "up and down", "tired but hopeful"]),
        "affect": rng.choice(["euthymic", "mildly constricted", "full-range", "dysthymic"]),
        "insight": rng.choice(["good", "fair", "intact"]),
        "judgment": rng.choice(["good", "fair", "intact"]),
        "famdx": rng.choice(["bipolar disorder", "major depression", "an anxiety disorder",
                             "alcohol use disorder"]),
        "meddx1": rng.choice(["hypertension", "type 2 diabetes mellitus", "hypothyroidism",
                              "gastroesophageal reflux disease"]),
        "meddx2": rng.choice(["hyperlipidemia", "migraine", "obstructive sleep apnea on CPAP",
                              "chronic low back pain"]),
        "alg": rng.choice(["penicillin", "sulfa", "codeine"]),
        "living": rng.choice(["their spouse and two children", "a roommate", "family", "alone with a dog"]),
        "job": rng.choice(["an accountant", "a teacher", "a software engineer", "a retail manager",
                           "a nurse"]),
        "support": rng.choice(["spouse and close friends", "siblings", "a faith community",
                               "longtime friends"]),
        "edu": rng.choice(["a bachelor's degree", "an associate degree", "a graduate degree"]),
        "tob": rng.choice(["never smoker", "former smoker, quit five years ago",
                           "current half-pack-per-day smoker"]),
        "mon": rng.choice(["last month", "six weeks ago", "the prior quarter"]),
        "ref": rng.choice(["intensive outpatient programming", "a sleep medicine evaluation",
                           "couples therapy", "a dietitian consultation"]),
        "n": str(rng.randint(2, 9)),
        "n0": str(rng.choice([20, 30, 40, 45, 60, 90])),
        "n2": str(rng.randint(4, 12)),
        "age": str(rng.randint(16, 34)),
        "glu": str(rng.randint(88, 118)),
        "a1c": f"{rng.uniform(5.2, 6.4):.1f}",
        "ldl": str(rng.randint(78, 148)),
        "vpa": str(rng.randint(52, 98)),
        "tsh": f"{rng.uniform(0.8, 4.2):.2f}",
        "lith": f"{rng.uniform(0.5, 1.0):.2f}",
        "bp": f"{rng.randint(104, 138)}/{rng.randint(64, 88)}",
        "hr": str(rng.randint(58, 96)),
        "wt": str(rng.randint(118, 242)),
        "bmi": f"{rng.uniform(19.5, 34.0):.1f}",
        "qtc": str(rng.randint(398, 462)),
        "phq": str(rng.randint(3, 14)),
        "phq2": str(rng.randint(9, 21)),
        "gad": str(rng.randint(2, 12)),
        "gad2": str(rng.randint(7, 17)),
        "prl": str(rng.randint(4, 38)),
    }
    return template.format(**subs)


def _med_paragraph(rng: random.Random, n_meds: int) -> str:
    chosen = rng.sample(list(MEDS.keys()), n_meds)
    lines = ["Medication review, reconciled against the pharmacy fill record:"]
    for drug in chosen:
        info = MEDS[drug]
        tmpl_status = rng.choice(MED_STATUS_TEMPLATES)
        _, _, tmpl = tmpl_status
        doses = info["doses"]
        dose = rng.choice(doses)
        dose0 = rng.choice([d for d in doses if d != dose] or doses)
        se = ""
        if rng.random() < 0.45:
            se = "; reports " + " and ".join(rng.sample(SIDE_EFFECTS, rng.randint(1, 2)))
        line = tmpl.format(
            drug=drug, dose=dose, dose0=dose0,
            route=info.get("route", "oral"),
            freq=rng.choice(info["freq"]),
            tol=rng.choice(["good", "acceptable", "excellent"]),
            reason=rng.choice(CHANGE_REASONS),
            target=rng.choice(["residual insomnia", "anxious distress", "low mood",
                               "attentional symptoms", "nightmares"]),
            taper=rng.choice(["four weeks", "six weeks", "two months"]),
            trialdur=rng.choice(["three months", "six months", "one year"]),
            switchto=rng.choice([d for d in MEDS if d != drug]),
            se=se,
        )
        lines.append(line)
    return " ".join(lines)


def make_note(rng: random.Random, tok, target_tokens: int, uid: str) -> str:
    """Assemble one note, tuned to ~target_tokens with the real tokenizer."""
    dx = rng.sample(DIAGNOSES, rng.randint(1, 3))
    header = (
        f"Encounter {uid}. Psychiatric medication management follow-up, "
        f"{rng.randint(20, 55)} minutes. Reason for visit: {rng.choice(['routine follow-up', 'medication adjustment', 'post-hospitalization transition', 'quarterly review'])}. "
        f"Active problems: {'; '.join(dx)}."
    )
    parts: list[str] = [header]
    if rng.random() < 0.6:
        parts.append(TELEHEALTH_BLOCK)
    parts.append("History of present illness: " +
                 " ".join(_fill(rng, s) for s in rng.sample(HPI_SENTENCES, 10)))
    parts.append(_med_paragraph(rng, rng.randint(8, 12)))
    parts.append("Pertinent history: " +
                 " ".join(_fill(rng, s) for s in rng.sample(HISTORY_SENTENCES, 7)))
    parts.append("Interval data and measurements: " +
                 " ".join(_fill(rng, s) for s in rng.sample(LAB_SENTENCES, 6)))
    parts.append("Mental status examination: " +
                 " ".join(_fill(rng, s) for s in rng.sample(MSE_SENTENCES, 9)))
    parts.append("Assessment and plan: " +
                 " ".join(_fill(rng, s) for s in rng.sample(PLAN_SENTENCES, 8)))

    # Pad with extra HPI/history detail until we reach the target token count.
    filler_pool = HPI_SENTENCES + HISTORY_SENTENCES + LAB_SENTENCES + PLAN_SENTENCES
    text = "\n\n".join(parts)
    n_tok = len(tok.encode(text).ids)
    while n_tok < target_tokens:
        extra = " ".join(_fill(rng, s) for s in rng.sample(filler_pool, 4))
        parts.insert(-1, "Additional interval detail: " + extra)
        text = "\n\n".join(parts)
        n_tok = len(tok.encode(text).ids)
    return text


def _worker(job: tuple[int, int, int, int, str]) -> list[str]:
    """Generate notes [start, start+count) as pre-serialized JSONL lines."""
    start, count, target_tokens, jitter, tok_path = job
    from tokenizers import Tokenizer
    tok = Tokenizer.from_file(tok_path)
    rng = random.Random(42_000 + start)   # deterministic per-chunk stream
    lines: list[str] = []
    for i in range(start, start + count):
        uid = f"{i:08d}-{rng.getrandbits(24):06x}"   # globally unique across workers
        target = target_tokens + rng.randint(-jitter, jitter)
        text = make_note(rng, tok, target, uid)
        n_tok = len(tok.encode(text).ids)
        lines.append(json.dumps({"text": text, "tokens": n_tok}))
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40000)
    ap.add_argument("--target-tokens", type=int, default=2900)
    ap.add_argument("--jitter", type=int, default=250,
                    help="uniform +/- jitter applied to the per-note token target")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    from huggingface_hub import hf_hub_download
    tok_path = hf_hub_download("google/medgemma-27b-text-it", "tokenizer.json")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUT_DIR / "notes.jsonl"
    t0 = time.time()
    chunk = 1000
    jobs = [(s, min(chunk, args.n - s), args.target_tokens, args.jitter, tok_path)
            for s in range(0, args.n, chunk)]
    counts: list[int] = []
    done = 0
    with out_file.open("w") as fh, ProcessPoolExecutor(max_workers=args.workers) as pool:
        for lines in pool.map(_worker, jobs):   # ordered -> deterministic file
            for line in lines:
                fh.write(line + "\n")
                counts.append(json.loads(line)["tokens"])
            done += len(lines)
            print(f"  {done}/{args.n} notes ({time.time()-t0:.0f}s)", flush=True)

    from tokenizers import Tokenizer
    tok = Tokenizer.from_file(tok_path)
    stats = {
        "n_notes": len(counts),
        "target_tokens": args.target_tokens,
        "mean_tokens": round(statistics.mean(counts), 1),
        "stdev_tokens": round(statistics.stdev(counts), 1),
        "min_tokens": min(counts),
        "p50_tokens": int(statistics.median(counts)),
        "max_tokens": max(counts),
        "tokenizer": "google/medgemma-27b-text-it",
        "system_prompt_tokens": len(tok.encode(SYSTEM_PROMPT_REAL).ids),
        "file": str(out_file),
        "generation_s": round(time.time() - t0, 1),
    }
    (OUT_DIR / "stats.json").write_text(json.dumps(stats, indent=2))
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
