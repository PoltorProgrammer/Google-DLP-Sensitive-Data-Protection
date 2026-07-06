import os
import io
import re
import time
import itertools
import fitz  # PyMuPDF
from google.cloud import dlp_v2
from google.cloud import vision
from google.cloud import translate_v3 as translate
from google.api_core import client_options as client_options_lib
from typing import List

# Full InfoTypes list used for EVERY inspection path (initial redaction,
# translation re-redaction and verification). Keep this the single source
# of truth so no path silently uses a weaker list.
DEFAULT_INFO_TYPES = [
    {"name": "PERSON_NAME"},
    {"name": "PHONE_NUMBER"},
    {"name": "EMAIL_ADDRESS"},
    {"name": "CREDIT_CARD_NUMBER"},
    {"name": "STREET_ADDRESS"},
    {"name": "PASSPORT"},

    # Germany
    {"name": "GERMANY_PASSPORT"},
    {"name": "GERMANY_IDENTITY_CARD_NUMBER"},
    {"name": "GERMANY_DRIVERS_LICENSE_NUMBER"},
    {"name": "GERMANY_TAXPAYER_IDENTIFICATION_NUMBER"},
    {"name": "GERMANY_SCHUFA_ID"},

    # Switzerland
    {"name": "SWITZERLAND_SOCIAL_SECURITY_NUMBER"},

    # Austria
    {"name": "AUSTRIA_SOCIAL_SECURITY_NUMBER"},

    # General / Other
    {"name": "IBAN_CODE"},
    {"name": "SWIFT_CODE"},
    {"name": "IMEI_HARDWARE_ID"},
    {"name": "IP_ADDRESS"}
]

# DLP dictionary size hard limit is ~128KB; stay well below it.
MAX_DICT_BYTES = 100000

# DLP inspect_content text payload limit is 0.5 MiB; chunk well below it.
MAX_TEXT_INSPECT_CHARS = 100000

IMAGE_BYTE_TYPES = {
    ".png": dlp_v2.ByteContentItem.BytesType.IMAGE_PNG,
    ".jpg": dlp_v2.ByteContentItem.BytesType.IMAGE_JPEG,
    ".jpeg": dlp_v2.ByteContentItem.BytesType.IMAGE_JPEG,
    ".bmp": dlp_v2.ByteContentItem.BytesType.IMAGE_BMP,
}


class ClinicalDocumentProcessor:
    def __init__(self, project_id: str, location: str = "global", credentials_file: str = None,
                 log_callback=None, translation_location: str = "us-central1",
                 allow_global_fallback: bool = False):
        self.project_id = project_id
        self.location = location or "global"
        self.translation_location = translation_location or "us-central1"
        self.log_callback = log_callback
        self.allow_global_fallback = allow_global_fallback
        # Some DLP features (image inspection) are unavailable in single regions
        # and require the multi-region ('europe'/'us'). Resolved lazily per feature.
        self._resolved_parents = {}

        if credentials_file:
            credentials_path = os.path.abspath(credentials_file)
            if not os.path.exists(credentials_path):
                raise FileNotFoundError(
                    f"Service account key not found: {credentials_path}. "
                    "Check google_cloud.service_account_key_file in config.json."
                )
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path

        # Data residency: all DLP content calls carry the configured location in
        # the request parent, so inspection happens in-region (e.g. europe-west6).
        self.dlp_parent = f"projects/{self.project_id}/locations/{self.location}"
        self.dlp_client = dlp_v2.DlpServiceClient()

        # Vision offers region-pinned endpoints; pick one matching the DLP region.
        vision_endpoint = None
        if self.location.startswith("europe"):
            vision_endpoint = "eu-vision.googleapis.com"
        elif self.location.startswith(("us", "northamerica")):
            vision_endpoint = "us-vision.googleapis.com"

        if vision_endpoint:
            opts = client_options_lib.ClientOptions(api_endpoint=vision_endpoint)
            self.vision_client = vision.ImageAnnotatorClient(client_options=opts)
        else:
            self.vision_client = vision.ImageAnnotatorClient()

        self.translate_client = translate.TranslationServiceClient()

    def log(self, message, metadata=None):
        # A failing log sink must never abort document processing
        try:
            if self.log_callback:
                self.log_callback(message, metadata)
            else:
                print(message)
        except Exception:
            try:
                print(message.encode("ascii", "replace").decode())
            except Exception:
                pass

    def _build_inspect_config(self, custom_terms: List[str] = None) -> dict:
        """Single source of truth for the inspection config used by every path."""
        inspect_config = {
            "info_types": list(DEFAULT_INFO_TYPES),
            "min_likelihood": dlp_v2.Likelihood.POSSIBLE
        }

        if custom_terms:
            expanded_terms = self._generate_term_combinations(custom_terms)

            current_size = sum(len(t.encode('utf-8')) for t in expanded_terms)
            if current_size > MAX_DICT_BYTES:
                self.log(f"Warning: Generated term list size ({current_size} bytes) exceeds safety limit ({MAX_DICT_BYTES}). Truncating...",
                         metadata={"original_count": len(expanded_terms)})

                # Keep the user's original terms first, then combinations, up to the byte limit.
                allowed_terms = []
                current_acc = 0
                priority_terms = set(custom_terms)
                other_terms = [t for t in expanded_terms if t not in priority_terms]

                for t in priority_terms:
                    size = len(t.encode('utf-8'))
                    if current_acc + size < MAX_DICT_BYTES:
                        allowed_terms.append(t)
                        current_acc += size

                for t in other_terms:
                    size = len(t.encode('utf-8'))
                    if current_acc + size < MAX_DICT_BYTES:
                        allowed_terms.append(t)
                        current_acc += size
                    else:
                        break

                expanded_terms = allowed_terms
                self.log(f"Truncated term list to {len(expanded_terms)} terms ({current_acc} bytes).")

            self.log(f"Expanded {len(custom_terms)} terms into {len(expanded_terms)} combinations...",
                     metadata={"term_count": len(expanded_terms)})

            inspect_config["custom_info_types"] = [{
                "info_type": {"name": "CUSTOM_REDACTION_LIST"},
                "likelihood": dlp_v2.Likelihood.VERY_LIKELY,
                "dictionary": {
                    "word_list": {"words": expanded_terms}
                }
            }]

        return inspect_config

    def process_document(self, filepath: str, custom_terms: List[str] = None, output_config: dict = None) -> dict:
        filename = os.path.basename(filepath)

        if output_config is None:
            output_config = {
                "redaction": True,
                "selectable_text_copy": True,
                "non_selectable_text_copy": False
            }

        inspect_config = self._build_inspect_config(custom_terms)

        is_pdf = filepath.lower().endswith(".pdf")

        try:
            if is_pdf:
                doc = fitz.open(filepath)
                return self._process_pdf_doc(doc, inspect_config, output_config)
            else:
                img_bytes = self._process_image(filepath, inspect_config)
                return {"selectable": img_bytes, "stats": {"pages": 1, "region": self.dlp_parent}}

        except Exception as e:
            self.log(f"Failed to redact {filename}: {e}")
            raise

    def process_bytes(self, file_bytes: bytes, custom_terms: List[str] = None, output_config: dict = None) -> dict:
        """Process a PDF from bytes (used to re-redact translated documents).
        Uses the exact same inspection config as process_document."""
        if output_config is None:
            output_config = {}

        inspect_config = self._build_inspect_config(custom_terms)

        try:
            doc = fitz.open("pdf", file_bytes)
            return self._process_pdf_doc(doc, inspect_config, output_config)
        except Exception as e:
            self.log(f"Failed to process bytes: {e}")
            raise

    def _generate_term_combinations(self, terms: List[str]) -> List[str]:
        """
        Generates smart combinations of terms.
        E.g. ["Jhon", "Smith"] -> "Jhon", "Smith", "JhonSmith", "SmithJhon", "J.Smith", "Smith.J", etc.
        Also includes simple OCR-like fuzzing (e.g. "Smith" -> "Smlth", "Jhon" -> "Jhan").
        Refined to split multi-word strings into atomic name parts for better recombination coverage.
        """
        results = set(terms)

        # 1. Distinguish between Text (Names) and Numbers (PIDs)
        # And decompose text into atomic parts
        all_text_parts = []
        all_numeric = []

        for t in terms:
            # Heuristic: If it contains a digit, treat it as an ID/Number (no names-recombination).
            if any(char.isdigit() for char in t):
                all_numeric.append(t)
            else:
                # Treat as text. Split into atomic parts (e.g. "Nour El Din, Omar" -> ["Nour", "El", "Din", "Omar"])
                parts = re.split(r'[\s,._-]+', t)
                for p in parts:
                    if len(p) > 0:
                        all_text_parts.append(p)
                        # Ensure atomic parts are also redacted individually if significant
                        if len(p) > 1:
                            results.add(p)

        # Remove duplicates from parts while preserving order
        unique_text = []
        seen = set()
        for x in all_text_parts:
            if x not in seen:
                unique_text.append(x)
                seen.add(x)

        # 2. Select Subset of Names for Complex Recombination (up to 6 atomic parts)
        subset_text = unique_text[:6]

        # 3. Generate Fuzz Variations (Names Only)
        fuzzed_text_terms = set(subset_text)
        for t in subset_text:
            variations = self._generate_fuzz_variations(t)
            fuzzed_text_terms.update(variations)

        # 4. Generate Combinations (Names Only)
        # We exclude numeric terms so PIDs don't get merged into Name Strings.
        valid_parts = [t for t in fuzzed_text_terms if len(t) > 0]

        # Permutations of length 2 up to 4 parts ("1, 2, 3 or 4 names")
        max_r = min(len(subset_text), 4) + 1

        def get_initial_or_full(p):
            return p[0] if p else ""

        for r in range(2, max_r):
            for parts in itertools.permutations(valid_parts, r):
                # Concat (NourElDinOmar)
                results.add("".join(parts))
                # Space (Nour El Din Omar)
                results.add(" ".join(parts))
                # Dot (Nour.El.Din.Omar)
                results.add(".".join(parts))
                # Underscore (Nour_El_Din_Omar)
                results.add("_".join(parts))
                # Comma (Nour, El, Din, Omar)
                results.add(", ".join(parts))

                # Pattern A: Initials for [:-1], Full for [-1]  e.g. "J. S. Mader"
                prefix_inits = [get_initial_or_full(p) for p in parts[:-1]]
                last_full = parts[-1]
                inits_A = prefix_inits + [last_full]

                if inits_A != list(parts):
                    results.add(".".join(inits_A))
                    results.add(" ".join(inits_A))
                    results.add("".join(inits_A))

                # Pattern B: Full for [0], Initials for [1:]  e.g. "Mader J. S."
                first_full = parts[0]
                suffix_inits = [get_initial_or_full(p) for p in parts[1:]]
                inits_B = [first_full] + suffix_inits

                if inits_B != list(parts) and inits_B != inits_A:
                    results.add(".".join(inits_B))
                    results.add(" ".join(inits_B))

        return list(results)

    def _generate_fuzz_variations(self, term: str) -> List[str]:
        """
        Generates simple misreadings/typos common in OCR.
        - i <-> l <-> 1
        - o <-> a <-> 0
        - e <-> 3
        """
        variations = set()

        substitutions = {
            'i': ['l', '1'],
            'l': ['i', '1'],
            'o': ['a', '0'],
            'a': ['o'],
            '0': ['o'],
            '1': ['i', 'l']
        }

        # Single Character Substitution
        for i, char in enumerate(term):
            lower_char = char.lower()
            if lower_char in substitutions:
                for sub in substitutions[lower_char]:
                    variant = term[:i] + sub + term[i+1:]
                    variations.add(variant)

                    if char.isupper():
                        variant_upper = term[:i] + sub.upper() + term[i+1:]
                        variations.add(variant_upper)

        return list(variations)

    @staticmethod
    def _is_unsupported_location_error(exc) -> bool:
        return "not supported in this location" in str(exc).lower()

    def _inspect_with_retry(self, request, attempts=3):
        """Retry transient DLP failures so one network blip doesn't abort a document.
        Deterministic errors (unsupported location) are raised immediately."""
        for attempt in range(attempts):
            try:
                return self.dlp_client.inspect_content(request=request)
            except Exception as e:
                if self._is_unsupported_location_error(e) or attempt == attempts - 1:
                    raise
                wait = 1.5 * (attempt + 1)
                self.log(f"       Transient DLP error (attempt {attempt+1}/{attempts}): {e}. Retrying in {wait:.0f}s...")
                time.sleep(wait)

    def _location_chain(self):
        """Candidate DLP locations, most specific first. Never leaves the configured
        jurisdiction (e.g. europe-west6 -> europe) unless global fallback is
        explicitly allowed in the configuration."""
        chain = [self.location]
        for prefix, multi in (("europe", "europe"),
                              ("northamerica", "us"), ("southamerica", "us"), ("us", "us"),
                              ("australia", "asia"), ("asia", "asia")):
            if self.location.startswith(prefix) and self.location != multi:
                if multi not in chain:
                    chain.append(multi)
                break
        if self.allow_global_fallback and "global" not in chain:
            chain.append("global")
        return chain

    def _with_location_fallback(self, feature, call):
        """Run `call(parent)` against the configured location, falling back through
        the multi-region (and optionally global) when DLP reports the feature is
        not supported there. The working location is cached per feature."""
        resolved = self._resolved_parents.get(feature)
        if resolved:
            parents = [resolved]
        else:
            parents = [f"projects/{self.project_id}/locations/{loc}" for loc in self._location_chain()]

        last_error = None
        for parent in parents:
            try:
                result = call(parent)
            except Exception as e:
                if self._is_unsupported_location_error(e):
                    last_error = e
                    continue
                raise
            if self._resolved_parents.get(feature) != parent:
                self._resolved_parents[feature] = parent
                loc = parent.rsplit("/", 1)[1]
                if loc != self.location:
                    if loc == "global":
                        self.log(f"⚠ {feature.capitalize()} inspection is not offered in {self.location}; "
                                 "using the GLOBAL endpoint (explicitly allowed in config).")
                    else:
                        self.log(f"🔒 {feature.capitalize()} inspection is not offered in {self.location}; "
                                 f"using the multi-region '{loc}' - data remains within the same jurisdiction.")
            return result

        hint = ("" if self.allow_global_fallback
                else " As a last resort you can set google_cloud.allow_global_fallback to true in config.json.")
        raise RuntimeError(
            f"DLP {feature} inspection is not supported in '{self.location}' or its multi-region."
            f" Choose a supported location.{hint}") from last_error

    def _inspect_with_fallback(self, feature, inspect_config, item):
        return self._with_location_fallback(feature, lambda parent: self._inspect_with_retry(
            request={"parent": parent, "inspect_config": inspect_config, "item": item}))

    def _process_image(self, filepath: str, inspect_config) -> bytes:
        with open(filepath, "rb") as f:
            image_bytes = f.read()
        ext = os.path.splitext(filepath)[1].lower()
        bytes_type = IMAGE_BYTE_TYPES.get(ext, dlp_v2.ByteContentItem.BytesType.IMAGE)
        return self._redact_image_bytes(image_bytes, inspect_config, bytes_type)

    def _redact_image_bytes(self, image_bytes: bytes, inspect_config,
                            bytes_type=dlp_v2.ByteContentItem.BytesType.IMAGE_PNG) -> bytes:
        """Native image redaction (returns modified pixels)"""
        image_redactions = []
        for it in inspect_config.get("info_types", []):
            image_redactions.append({"info_type": it, "redaction_color": {"red": 0, "green": 0, "blue": 0}})
        if "custom_info_types" in inspect_config:
            for cit in inspect_config["custom_info_types"]:
                image_redactions.append({"info_type": cit["info_type"], "redaction_color": {"red": 0, "green": 0, "blue": 0}})

        byte_item = {"type_": bytes_type, "data": image_bytes}
        response = self._with_location_fallback("image", lambda parent: self.dlp_client.redact_image(
            request={
                "parent": parent,
                "inspect_config": inspect_config,
                "image_redactions": image_redactions,
                "byte_item": byte_item
            }
        ))
        return response.redacted_image

    def _process_pdf_doc(self, doc, inspect_config, output_config) -> dict:
        """Redact an open fitz.Document and produce the configured outputs.
        Returns {"selectable": bytes?, "non_selectable": bytes?, "stats": dict}."""
        total_pages = len(doc)

        flattened_doc = fitz.open()

        self.log(f"Processing PDF Doc (Anonymizing + Flattening)...", metadata={"pages": total_pages})

        zoom = 3.0
        mat = fitz.Matrix(zoom, zoom)

        should_redact = output_config.get("redaction", True)
        iterations = output_config.get("redaction_iterations", 1)
        if iterations < 1: iterations = 1

        stats = {
            "pages": total_pages,
            "region": self.dlp_parent,
            "redaction_enabled": should_redact,
            "total_findings": 0,
            "findings_per_pass": [0] * iterations
        }

        for i in range(total_pages):
            page = doc.load_page(i)
            self.log(f"Analyzing & Digitalizing Page {i+1}/{total_pages}...")

            try:
                # STAGE 1: NATIVE REDACTION
                if should_redact:
                    for n in range(iterations):
                        pass_label = f"Pass {n+1}/{iterations}"

                        pix = page.get_pixmap(matrix=mat)
                        img_bytes = pix.tobytes("png")

                        item = {"byte_item": {"type_": dlp_v2.ByteContentItem.BytesType.IMAGE_PNG, "data": img_bytes}}
                        response = self._inspect_with_fallback("image", inspect_config, item)

                        findings = response.result.findings
                        if findings:
                            stats["total_findings"] += len(findings)
                            stats["findings_per_pass"][n] += len(findings)
                            self.log(f"       [{pass_label}] Found {len(findings)} sensitive items. Applying native redactions...")
                            for finding in findings:
                                for loc in finding.location.content_locations:
                                    image_loc = getattr(loc, "image_location", None)
                                    if image_loc and image_loc.bounding_boxes:
                                        for box in image_loc.bounding_boxes:
                                            # Translate coordinates back to PDF points
                                            rect = fitz.Rect(box.left / zoom, box.top / zoom,
                                                            (box.left + box.width) / zoom, (box.top + box.height) / zoom)
                                            page.add_redact_annot(rect, fill=(0, 0, 0))
                            page.apply_redactions()
                        else:
                            self.log(f"       [{pass_label}] No findings.")
                            break

                # STAGE 2: FLATTENING & BURNING
                pix_redacted = page.get_pixmap(matrix=mat)
                redacted_img_bytes = pix_redacted.tobytes("png")

                new_page = flattened_doc.new_page(width=page.rect.width, height=page.rect.height)
                new_page.insert_image(page.rect, stream=redacted_img_bytes)

            except Exception as e:
                # A skipped page would silently disappear from the output - a missing
                # page in a clinical document is worse than a failed file. Abort.
                raise RuntimeError(
                    f"Page {i+1}/{total_pages} could not be processed safely: {e}. "
                    "Document aborted so no output with missing pages is saved."
                ) from e

            self.log(f"Page {i+1} completed", metadata={"page_done": i+1})

        if len(flattened_doc) != total_pages:
            raise RuntimeError(
                f"Output has {len(flattened_doc)} pages but the input has {total_pages} - aborting."
            )

        # Report the location that actually processed the page images
        stats["region"] = self._resolved_parents.get("image", self.dlp_parent)

        # --- GENERATE OUTPUTS ---
        results = {"stats": stats}

        # 1. Non-Selectable (Flattened Only)
        flattened_doc.set_metadata({})
        out_stream_flat = io.BytesIO()
        flattened_doc.save(out_stream_flat, garbage=4, deflate=True)
        flattened_bytes = out_stream_flat.getvalue()

        if output_config.get("non_selectable_text_copy", False):
            results["non_selectable"] = flattened_bytes

        # 2. Selectable (OCR Overlay)
        if output_config.get("selectable_text_copy", True):
            self.log("Applying Cloud OCR Overlay for Selectability...")
            selectable_doc = fitz.open("pdf", flattened_bytes)

            for i in range(len(selectable_doc)):
                page = selectable_doc.load_page(i)
                page_img_bytes = page.get_pixmap(matrix=mat).tobytes("png")

                try:
                     # STAGE 3: CLOUD OCR OVERLAY
                     vision_image = vision.Image(content=page_img_bytes)
                     vision_response = self.vision_client.document_text_detection(image=vision_image)

                     if vision_response.full_text_annotation:
                        # Place a hidden text layer
                        for page_v in vision_response.full_text_annotation.pages:
                            for block in page_v.blocks:
                                for paragraph in block.paragraphs:
                                    # Group words into lines within the paragraph
                                    # so PDF viewers reconstruct logical lines for selection/copy-paste

                                    # 1. Collect all words with their bounding boxes
                                    word_data = []
                                    for word in paragraph.words:
                                        word_text = "".join([l.text for l in word.symbols])
                                        vertices = word.bounding_box.vertices

                                        # Screen coordinates (still zoomed)
                                        vx = [v.x for v in vertices]
                                        vy = [v.y for v in vertices]
                                        x0, y0 = min(vx), min(vy)
                                        x1, y1 = max(vx), max(vy)

                                        y_center = (y0 + y1) / 2
                                        height = y1 - y0

                                        word_data.append({
                                            "text": word_text,
                                            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                                            "yc": y_center, "h": height
                                        })

                                    if not word_data:
                                        continue

                                    # 2. Group by Line (simple Y-center clustering)
                                    word_data.sort(key=lambda w: w["yc"])

                                    lines = []
                                    current_line = [word_data[0]]

                                    for w in word_data[1:]:
                                        prev = current_line[-1]
                                        avg_h = (prev["h"] + w["h"]) / 2
                                        if abs(w["yc"] - prev["yc"]) < (avg_h * 0.5):
                                            current_line.append(w)
                                        else:
                                            lines.append(current_line)
                                            current_line = [w]
                                    if current_line:
                                        lines.append(current_line)

                                    # 3. Process each line
                                    for line_words in lines:
                                        line_words.sort(key=lambda w: w["x0"])

                                        full_line_text = " ".join([w["text"] for w in line_words])

                                        line_y1_zoomed = max(w["y1"] for w in line_words)
                                        line_x0_zoomed = min(w["x0"] for w in line_words)
                                        line_y0_zoomed = min(w["y0"] for w in line_words)

                                        # Convert to PDF coordinates (un-zoom)
                                        x_ins = line_x0_zoomed / zoom
                                        y_ins = line_y1_zoomed / zoom

                                        h_zoomed = line_y1_zoomed - line_y0_zoomed
                                        font_size = (h_zoomed / zoom) * 0.8

                                        page.insert_text((x_ins, y_ins), full_line_text, fontsize=font_size, render_mode=3)
                except Exception as e:
                    self.log(f"       OCR Warning on page {i+1}: {e}")

            # Save Selectable
            selectable_doc.set_metadata({})
            out_stream_sel = io.BytesIO()
            selectable_doc.save(out_stream_sel, garbage=4, deflate=True)
            results["selectable"] = out_stream_sel.getvalue()
            selectable_doc.close()

        doc.close()
        flattened_doc.close()

        generated = [k for k in results.keys() if k != "stats"]
        self.log(f"Processing Complete. Generated: {generated}", metadata={"save_done": True})
        return results

    def verify_output(self, pdf_bytes: bytes, custom_terms: List[str] = None) -> dict:
        """
        Post-redaction safety net: extracts the (OCR) text layer of a finished
        output and checks for residual sensitive data.
        - Custom keywords are checked locally (exact, case-insensitive) so the
          terms never leave the machine again.
        - InfoTypes are re-checked via DLP text inspection at LIKELY threshold
          to keep false positives low.
        Returns {"pages", "dlp_findings", "dlp_by_infotype", "keyword_hits"}.
        """
        result = {"pages": 0, "dlp_findings": 0, "dlp_by_infotype": {}, "keyword_hits": 0}

        doc = fitz.open("pdf", pdf_bytes)
        result["pages"] = len(doc)
        full_text = "\n".join(page.get_text() for page in doc)
        doc.close()

        if not full_text.strip():
            return result

        # 1. Local keyword check (counts only; terms are never logged)
        if custom_terms:
            lowered = full_text.lower()
            for term in custom_terms:
                t = term.strip().lower()
                if len(t) >= 2:
                    result["keyword_hits"] += lowered.count(t)

        # 2. DLP text inspection at higher likelihood to reduce noise
        verify_config = {
            "info_types": list(DEFAULT_INFO_TYPES),
            "min_likelihood": dlp_v2.Likelihood.LIKELY
        }

        for start in range(0, len(full_text), MAX_TEXT_INSPECT_CHARS):
            chunk = full_text[start:start + MAX_TEXT_INSPECT_CHARS]
            response = self._inspect_with_fallback("text", verify_config, {"value": chunk})
            for finding in response.result.findings:
                result["dlp_findings"] += 1
                name = finding.info_type.name
                result["dlp_by_infotype"][name] = result["dlp_by_infotype"].get(name, 0) + 1

        return result

    def ocr_words(self, image_bytes: bytes, zoom: float = 1.0) -> List[dict]:
        """OCR a page image and return words with bounding boxes in page
        coordinates (divided back by the render zoom). Used by the click-to-tag
        preview for scanned documents; the caller must keep results in memory
        only - this is unredacted text."""
        vision_image = vision.Image(content=image_bytes)
        response = self.vision_client.document_text_detection(image=vision_image)

        words_out = []
        if response.full_text_annotation:
            for page_v in response.full_text_annotation.pages:
                for block in page_v.blocks:
                    for paragraph in block.paragraphs:
                        for word in paragraph.words:
                            text = "".join(s.text for s in word.symbols)
                            if not text.strip():
                                continue
                            vs = word.bounding_box.vertices
                            xs = [v.x for v in vs]
                            ys = [v.y for v in vs]
                            words_out.append({
                                "text": text,
                                "x0": min(xs) / zoom, "y0": min(ys) / zoom,
                                "x1": max(xs) / zoom, "y1": max(ys) / zoom,
                            })
        return words_out

    def translate_document(self, doc_bytes: bytes, target_language: str = "en") -> List[tuple]:
        """
        Translates a PDF document using Google Cloud Translation AI.
        Dynamically splits the document into chunks where each chunk is < 30MB
        (to stay well within Google's 40MiB synchronous payload limit).
        NOTE: Document translation is processed in self.translation_location
        (default us-central1) — outside the EU. Only already-redacted bytes are sent.
        """
        try:
            doc = fitz.open("pdf", doc_bytes)
            total_pages = len(doc)
            results = []

            MAX_PAYLOAD_BYTES = 30 * 1024 * 1024  # 30MB extra-safe limit (API limit is 40MiB)

            self.log(f"Analyzing {total_pages} pages for dynamic chunking...")

            current_chunk_doc = fitz.open()
            current_start_idx = 0
            chunk_num = 1

            for i in range(total_pages):
                self.log(f"Preparing Page {i+1}...", metadata={"trans_flatten_start": True})
                # We flatten page-by-page to check size
                page = doc.load_page(i)
                zoom = 2.0  # High quality
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat)
                img_bytes = pix.tobytes("png")

                # Try adding to current chunk
                temp_page = current_chunk_doc.new_page(width=page.rect.width, height=page.rect.height)
                temp_page.insert_image(page.rect, stream=img_bytes)
                self.log(f"Page {i+1} flattened.", metadata={"trans_flatten_done": True})

                # Check resulting size
                current_size = len(current_chunk_doc.tobytes())

                if current_size > MAX_PAYLOAD_BYTES and i > current_start_idx:
                    # Current page pushed us over the limit
                    current_chunk_doc.delete_page(len(current_chunk_doc) - 1)

                    # Finalize previous chunk
                    chunk_label = f"{current_start_idx+1:02d}-{i:02d}"
                    chunk_bytes = current_chunk_doc.tobytes()
                    self.log(f"Sending Chunk {chunk_num} (Pages {chunk_label}, {round(len(chunk_bytes)/(1024*1024), 1)}MB) to API...")

                    self.log(f"Translating...", metadata={"trans_api_start": len(chunk_bytes)})
                    translated_bytes = self._call_translate_api(chunk_bytes, target_language)
                    self.log(f"Chunk {chunk_num} completed.", metadata={"trans_api_done": True})

                    results.append((chunk_label, translated_bytes))

                    # Start new chunk with the current page
                    current_chunk_doc.close()
                    current_chunk_doc = fitz.open()
                    current_start_idx = i
                    chunk_num += 1

                    self.log(f"Retrying Page {i+1} in new chunk...", metadata={"trans_flatten_start": True})
                    new_temp_page = current_chunk_doc.new_page(width=page.rect.width, height=page.rect.height)
                    new_temp_page.insert_image(page.rect, stream=img_bytes)
                    self.log(f"Page {i+1} moved to new chunk.", metadata={"trans_flatten_done": True})

            # Send the final chunk
            if len(current_chunk_doc) > 0:
                chunk_label = f"{current_start_idx+1:02d}-{total_pages:02d}"
                chunk_bytes = current_chunk_doc.tobytes()
                # If it's the only chunk, we don't need the label
                actual_label = "" if chunk_num == 1 else chunk_label

                self.log(f"Sending Final Chunk (Pages {chunk_label}, {round(len(chunk_bytes)/(1024*1024), 1)}MB) to API...")
                self.log(f"Translating...", metadata={"trans_api_start": len(chunk_bytes)})
                translated_bytes = self._call_translate_api(chunk_bytes, target_language)
                self.log(f"Final chunk completed.", metadata={"trans_api_done": True})

                results.append((actual_label, translated_bytes))

            current_chunk_doc.close()
            doc.close()
            return results

        except Exception as e:
            self.log(f"Dynamic translation failed: {e}")
            raise

    def _call_translate_api(self, doc_bytes: bytes, target_language: str) -> bytes:
        """Internal helper to call the Google Translation API for a single PDF byte stream."""
        # Document translation is currently only supported in 'us-central1' or 'global'.
        parent = f"projects/{self.project_id}/locations/{self.translation_location}"

        document_input_config = {
            "content": doc_bytes,
            "mime_type": "application/pdf",
        }

        response = self.translate_client.translate_document(
            request={
                "parent": parent,
                "target_language_code": target_language,
                "document_input_config": document_input_config,
            }
        )

        doc_trans = response.document_translation
        if hasattr(doc_trans, "byte_content") and doc_trans.byte_content:
            return doc_trans.byte_content
        elif hasattr(doc_trans, "content") and doc_trans.content:
            return doc_trans.content
        elif hasattr(doc_trans, "byte_stream_outputs") and doc_trans.byte_stream_outputs:
            return b"".join(doc_trans.byte_stream_outputs)
        else:
            raise AttributeError("Could not extract bytes from DocumentTranslation response.")

    def merge_pdf_bytes(self, pdf_bytes_list: List[bytes]) -> bytes:
        """Merges a list of PDF bytes into a single PDF document."""
        merged_doc = fitz.open()

        self.log(f"Merging {len(pdf_bytes_list)} PDF chunks into single document...")

        for i, pdf_bytes in enumerate(pdf_bytes_list):
            try:
                with fitz.open("pdf", pdf_bytes) as doc:
                    merged_doc.insert_pdf(doc)
            except Exception as e:
                self.log(f"Warning: Failed to merge chunk {i+1}: {e}")

        out_stream = io.BytesIO()
        merged_doc.save(out_stream, garbage=4, deflate=True)
        merged_bytes = out_stream.getvalue()
        merged_doc.close()
        return merged_bytes