import os
import httpx
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("FPT_API_KEY")
base_url = os.getenv("FPT_BASE_URL", "https://mkp-api.fptcloud.com").rstrip("/")
model_name = os.getenv("FPT_MODEL_NAME", "gpt-oss-120b")

url = f"{base_url}/chat/completions"
headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
}

# payload = {
#     "model": model_name,
#     "messages": [
#         {"role": "system", "content": "You are a concise research extractor. Return JSON only."},
#         {"role": "user", "content": "Return a JSON object with key 'status' and value 'ok'."},
#     ],
#     "temperature": 0.0,
# }

print(f"Connecting to: {url} with model: {model_name}...")

import json

# PMID: 35084428 Summary
SAMPLE_ABSTRACT = """
Background: Progressive resistance training (PRT) is considered a cornerstone intervention for frailty in older adults.
Methods: We conducted a randomized controlled trial with 120 frail individuals aged 75 years and older. Participants were randomized to either a 12-week high-intensity PRT program (3 sessions/week) or usual care. The primary outcome was frailty status measured by the Fried Frailty Phenotype.
Results: At 12 weeks, the PRT group demonstrated a statistically significant reduction in frailty scores compared to the control group (mean difference -1.2 points, 95% CI -1.8 to -0.6, p < 0.01). Gait speed improved by 0.15 m/s. No serious adverse events were reported during the intervention.
Conclusions: A 12-week progressive resistance training program is safe and significantly reduces frailty severity in community-dwelling older adults.
"""

SAMPLE_ABSTRACT_MISSING_SAMPLE_SIZE = """
Background: Progressive resistance training (PRT) is considered a cornerstone intervention for frailty in older adults.
Methods: We conducted a randomized controlled trial with frail individuals aged 75 years and older. Participants were randomized to either a 12-week high-intensity PRT program (3 sessions/week) or usual care. The primary outcome was frailty status measured by the Fried Frailty Phenotype.
Results: At 12 weeks, the PRT group demonstrated a statistically significant reduction in frailty scores compared to the control group (mean difference -1.2 points, 95% CI -1.8 to -0.6, p < 0.01). Gait speed improved by 0.15 m/s. No serious adverse events were reported during the intervention.
Conclusions: A 12-week progressive resistance training program is safe and significantly reduces frailty severity in community-dwelling older adults.
"""

EXTRACTION_SCHEMA = """
{
  "study_type": "string",
  "sample_size": "integer or null",
  "population": "string",
  "main_finding": "string",
  "supporting_passage": "full sentence copied verbatim from the abstract, ending with a period"
}
"""

payload = {
    "model": model_name,
    "messages": [
        {
            "role": "system",
            "content": (
                "You are an evidence extraction assistant. Extract structured medical evidence from the abstract.\n\n"
                "CRITICAL EXTRACTION RULES:\n"
                "1. Return ONLY a valid JSON object conforming to the schema.\n"
                "2. 'supporting_passage' must be a COMPLETE, FULL sentence from the abstract.\n"
                "3. NEVER use ellipses ('...'). Copy every word, number, and punctuation until the final period (.).\n"
                "4. If no sample size is reported, use null."
            ),

        },
        {
            "role": "user",
            "content": (
                f"Schema:\n{EXTRACTION_SCHEMA}\n\n"
                f"Abstract:\n{SAMPLE_ABSTRACT_MISSING_SAMPLE_SIZE}\n\n"
                "Extract the JSON:"
            ),
        },
    ],
    # "response_format": {"type": "json_object"},
    "temperature": 0.0,
}


try:
    with httpx.Client(timeout=30.0) as client:
        response = client.post(url, headers=headers, json=payload)
        print(f"HTTP Status: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            choice = data["choices"][0]
            raw_content = choice["message"]["content"]
            cleaned_content = raw_content.strip()
            if cleaned_content.startswith("```"):
                lines = cleaned_content.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                cleaned_content = "\n".join(lines).strip()

                        # Check generation finish state
            finish_reason = choice.get("finish_reason")

            print(f"Finish reason: {finish_reason}")
            print(f"Actual model returned: {data.get('model')}")
            print("Usage:", data.get("usage", {}))

            try:
                extracted = json.loads(cleaned_content)
                print("\n--- Parsed JSON Successfully ---")
                print(json.dumps(extracted, indent=2, ensure_ascii=False))

                # Validate sample_size explicitly (handling null vs missing key)
                if "sample_size" not in extracted:
                    print("\n❌ FAIL (Missing Key): Model omitted 'sample_size' field in JSON!")
                elif extracted["sample_size"] is None:
                    print("\n✅ PASS: 'sample_size' is present and correctly null.")
                else:
                    actual_sample_size = extracted["sample_size"]
                    print(f"\n❌ FAIL (Hallucination): Model predicted sample_size = {actual_sample_size} despite missing in abstract!")

                # Validate supporting_passage grounding
                passage = extracted.get("supporting_passage", "")
                if passage:
                    if passage in SAMPLE_ABSTRACT_MISSING_SAMPLE_SIZE:
                        print("✅ PASSAGE GROUNDED: Verbatim match in variant abstract.")
                    else:
                        print(f"❌ PASSAGE NOT FOUND verbatim in variant abstract:\n'{passage}'")
                else:
                    print("ℹ️ No supporting_passage returned.")

            except json.JSONDecodeError as e:
                print(f"\n❌ JSON Decode Error: {e}")
                print("Raw Content received:\n", raw_content)

except Exception as e:
    print(f"Connection failed: {e}")
