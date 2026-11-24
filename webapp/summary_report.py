"""Fill the institution's Word summary template without rewriting its package parts."""

from collections import Counter
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
import json
import re
from zipfile import ZipFile

from fastapi import HTTPException
from lxml import etree

from .programs import PROGRAM_CHAIRS

SEMESTERS = ("1st", "2nd", "Termbreak")
WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WORD14 = "http://schemas.microsoft.com/office/word/2010/wordml"
NS = {"w": WORD}
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
CILO_CHOICES = ("Attained", "Needs Improvement", "Not Attained")


def read_metadata(form):
    program = form.get("program")
    semester = form.get("semester")
    if program not in PROGRAM_CHAIRS or semester not in SEMESTERS:
        raise HTTPException(422, "Select a program and semester to generate the Word summary.")

    def field(key, label, limit):
        value = form.get(key)
        value = value.strip() if isinstance(value, str) else ""
        if not value or len(value) > limit or re.search(r"[\x00-\x1f\x7f\ufffe\uffff]", value):
            raise HTTPException(422, f"Enter {label} of up to {limit} characters for the Word summary.")
        return value

    return program, semester, field("course", "a course or subject", 120), field("school_year", "a school year", 30)


def rated(values):
    if not values:
        return "", ""
    score = (Decimal(sum(values)) / len(values)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    for threshold, interpretation in (("3.26", "Strongly Agree"), ("2.51", "Agree"),
                                      ("1.76", "Disagree"), ("1.00", "Strongly Disagree")):
        if score >= Decimal(threshold):
            return str(score), interpretation


def word(name):
    return f"{{{WORD}}}{name}"


def add_text(paragraph, value, props=None):
    run = etree.SubElement(paragraph, word("r"))
    if props is None:
        props = paragraph.find("w:pPr/w:rPr", NS)
    if props is not None:
        run.append(deepcopy(props))
    if value in {"\t", "\n"}:
        etree.SubElement(run, word("tab" if value == "\t" else "br"))
    else:
        text = etree.SubElement(run, word("t"))
        text.set(XML_SPACE, "preserve")
        text.text = value


def fill_line(paragraph, left, right, separator="\t"):
    props = paragraph.find("w:r/w:rPr", NS)
    properties = paragraph.find("w:pPr", NS)
    tabs = etree.Element(word("tabs"))
    properties.insert(0, tabs)
    stop = etree.SubElement(tabs, word("tab"))
    stop.set(word("val"), "left")
    stop.set(word("pos"), "4500")
    for node in list(paragraph):
        if node.tag in {word("r"), word("proofErr")}:
            paragraph.remove(node)
    add_text(paragraph, left, props)
    add_text(paragraph, separator, props)
    add_text(paragraph, right, props)


def fill_rating_table(table, bundle, votes):
    keys = iter(key for key in bundle.fields if key.startswith("Q"))
    by_section = {}
    for key, definition in bundle.fields.items():
        if key.startswith("Q"):
            by_section.setdefault(definition["section"], []).append(key)
    section_rows = {1, 8, 14, 22, 29, 36}
    sections = iter(by_section.values())
    rows = table.findall("w:tr", NS)
    if len(rows) != 40:
        raise ValueError("Rating table does not match the summary template")
    for index, row in enumerate(rows[1:], 1):
        cells = [cell.find("w:p", NS) for cell in row.findall("w:tc", NS)]
        if index in section_rows:
            numbers = [value for key in next(sections) for value in votes[key]]
        else:
            key = next(keys)
            if key in {"Q14", "Q15", "Q16", "Q17", "Q18"}:
                wording = cells[0].findall(".//w:t", NS)
                wording[0].text = bundle.fields[key]["statement"]
                for node in wording[1:]:
                    node.text = ""
            numbers = votes[key]
        score, interpretation = rated(numbers)
        if score:
            add_text(cells[1], score)
            add_interpretation(cells[2], interpretation)
    return rated([value for series in votes.values() for value in series])


def center(paragraph):
    props = paragraph.find("w:pPr", NS)
    style = props.find("w:rPr", NS)
    alignment = props.find("w:jc", NS)
    if alignment is None:
        alignment = etree.Element(word("jc"))
        if style is None:
            props.append(alignment)
        else:
            style.addprevious(alignment)
    alignment.set(word("val"), "center")


def add_interpretation(paragraph, interpretation):
    center(paragraph)
    props = deepcopy(paragraph.find("w:pPr/w:rPr", NS))
    for tag in ("sz", "szCs"):
        size = props.find(f"w:{tag}", NS)
        if size is not None:
            size.set(word("val"), "18")
    add_text(paragraph, interpretation, props)


def fill_cilos(table, bundle, responses):
    rows = table.findall("w:tr", NS)[1:]
    counts = [Counter(page.get(key) for page in responses) for key in bundle.cilos]
    last_marked = max((i for i, count in enumerate(counts, 1) if any(count[c] for c in CILO_CHOICES)), default=0)
    while len(rows) < last_marked:
        row = deepcopy(rows[-1])
        for child in row.iter():
            for attribute in ("paraId", "textId"):
                child.attrib.pop(f"{{{WORD14}}}{attribute}", None)
        table.append(row)
        rows.append(row)
    for row, count in zip(rows, counts):
        for cell, choice in zip(row.findall("w:tc", NS)[1:], CILO_CHOICES):
            paragraph = cell.find("w:p", NS)
            center(paragraph)
            add_text(paragraph, str(count[choice]))


def generate_report(template, bundle, pages, metadata):
    program, semester, course, school_year = metadata
    responses = [json.loads(page[4]) for page in pages if page[3] != "Failed"]
    if not responses:
        raise HTTPException(422, "No questionnaires could be read. Check the PDF before generating a summary.")
    votes = {
        key: [int(page[key]) for page in responses if page.get(key) in ("1", "2", "3", "4")]
        for key in bundle.fields if key.startswith("Q")
    }

    with ZipFile(template) as source:
        document = etree.fromstring(source.read("word/document.xml"))
        body = document.find("w:body", NS)
        paragraphs = body.findall("w:p", NS)
        rating_table, cilo_table = body.findall("w:tbl", NS)
        fill_line(paragraphs[5], f"Program : {program}", f"Course/Subject: {course}",
                  separator="\n" if len(course) > 33 else "\t")
        fill_line(paragraphs[6], f"Semester: {semester}", f"School Year: {school_year}")
        overall, description = fill_rating_table(rating_table, bundle, votes)
        if overall:
            add_text(rating_table.getnext(), f"Overall rating: {overall} — {description}")
        fill_cilos(cilo_table, bundle, responses)
        for paragraph, value in zip(paragraphs[28:31], (PROGRAM_CHAIRS[program], "Department Chair", program)):
            add_text(paragraph, value)
        paragraphs[31].find("w:pPr/w:spacing", NS).set(word("before"), "100")

        result = BytesIO()
        updated = etree.tostring(document, encoding="UTF-8", xml_declaration=True, standalone=True)
        with ZipFile(result, "w") as archive:
            for part in source.infolist():
                archive.writestr(part, updated if part.filename == "word/document.xml" else source.read(part))
    return result.getvalue()
