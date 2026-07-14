from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
import argparse
import sys

import rdflib
from rdflib import RDF, RDFS, URIRef


@dataclass(frozen=True)
class FieldDef:
    field: str
    comment: tuple[str, ...]
    types: tuple[str, ...]


@dataclass(frozen=True)
class ClassDef:
    comment: tuple[str, ...]
    parents: tuple[str, ...]
    fields: tuple[FieldDef, ...]


class RDFCodeGenerator(ABC):
    def _qname_or_iri(self, g: rdflib.Graph, term: URIRef) -> str:
        try:
            return g.namespace_manager.normalizeUri(term)
        except Exception:
            return str(term)

    def clean_comment(self, text: str) -> str:
        return " ".join(text.split()).replace("*/", "* /")

    def parse_ttl(
        self,
        ttl_url: str,
        namespace_url: str,
    ) -> dict[str, ClassDef]:
        schema = rdflib.Namespace(namespace_url)
        g = rdflib.Graph()
        g.parse(ttl_url, format="turtle")

        classes: set[URIRef] = set()
        props_by_class: dict[URIRef, set[URIRef]] = defaultdict(set)
        range_by_prop: dict[URIRef, set[URIRef]] = defaultdict(set)
        class_comments: dict[URIRef, list[str]] = defaultdict(list)
        prop_comments: dict[URIRef, list[str]] = defaultdict(list)
        parents_by_class: dict[URIRef, set[URIRef]] = defaultdict(set)

        for s in g.subjects(RDF.type, RDFS.Class):
            if isinstance(s, URIRef) and str(s).startswith(str(schema)):
                classes.add(s)

        # fecha a hierarquia antes de gerar saída
        queue = list(classes)
        while queue:
            cls = queue.pop()
            for parent in g.objects(cls, RDFS.subClassOf):
                if isinstance(parent, URIRef) and str(parent).startswith(str(schema)):
                    if parent not in classes:
                        classes.add(parent)
                        queue.append(parent)
                    parents_by_class[cls].add(parent)

        for cls in classes:
            for c in g.objects(cls, RDFS.comment):
                if isinstance(c, rdflib.Literal):
                    class_comments[cls].append(str(c))

        for prop, cls in g.subject_objects(schema.domainIncludes):
            if isinstance(prop, URIRef) and isinstance(cls, URIRef):
                if str(prop).startswith(str(schema)) and str(cls).startswith(str(schema)):
                    props_by_class[cls].add(prop)

        for prop, rng in g.subject_objects(schema.rangeIncludes):
            if isinstance(prop, URIRef) and isinstance(rng, URIRef):
                if str(prop).startswith(str(schema)) and str(rng).startswith(str(schema)):
                    range_by_prop[prop].add(rng)

        all_props = {p for props in props_by_class.values() for p in props}
        for prop in all_props:
            for c in g.objects(prop, RDFS.comment):
                if isinstance(c, rdflib.Literal):
                    prop_comments[prop].append(str(c))

        out: dict[str, ClassDef] = {}
        for cls in sorted(classes, key=str):
            class_key = self._qname_or_iri(g, cls)
            fields: list[FieldDef] = []

            for prop in sorted(props_by_class.get(cls, set()), key=str):
                fields.append(
                    FieldDef(
                        field=self._qname_or_iri(g, prop),
                        comment=tuple(dict.fromkeys(prop_comments.get(prop, []))),
                        types=tuple(sorted({
                            self._qname_or_iri(g, t) for t in range_by_prop.get(prop, set())
                        })),
                    )
                )

            out[class_key] = ClassDef(
                comment=tuple(dict.fromkeys(class_comments.get(cls, []))),
                parents=tuple(
                    self._qname_or_iri(g, p)
                    for p in sorted(parents_by_class.get(cls, set()), key=str)
                ),
                fields=tuple(fields),
            )

        return out

    def generate(self, chosen: list[str] | None = None, **kwargs) -> str:
        chosen = chosen or []
        out = self.parse_ttl(**kwargs)

        if not chosen:
            return self._generate(out)

        filtered: dict[str, ClassDef] = {}
        visited: set[str] = set()

        def collect(key: str) -> None:
            if key in visited or key not in out:
                return
            visited.add(key)
            filtered[key] = out[key]

            deps = set(out[key].parents)
            for field in out[key].fields:
                deps.update(field.types)

            for dep in deps:
                collect(dep)

        for item in chosen:
            collect(item)

        return self._generate(filtered)

    @abstractmethod
    def _generate(self, defs: dict[str, ClassDef]) -> str:
        ...


class TypescriptGenerator(RDFCodeGenerator):
    primitive_ts_types = {
        "schema:Boolean": "boolean",
        "schema:Date": "string",
        "schema:DateTime": "string",
        "schema:Number": "number",
        "schema:Float": "number",
        "schema:Integer": "number",
        "schema:Quantity": "number",
        "schema:Distance": "number",
        "schema:Duration": "number",
        "schema:Energy": "number",
        "schema:Mass": "number",
        "schema:Text": "string",
        "schema:CssSelectorType": "string",
        "schema:PronounceableText": "string",
        "schema:URL": "string",
        "schema:XPathType": "string",
        "schema:Time": "string",
    }

    def schema_to_ts_types(self, org: dict[str, ClassDef]) -> dict[str, ClassDef]:
        def as_ts_class(name: str) -> str:
            name = name.removeprefix("schema:")
            return f"Schema{name}"

        def as_ts_field(name: str) -> str:
            return name.removeprefix("schema:")

        result: dict[str, ClassDef] = {}

        for class_name, data in org.items():
            if class_name in self.primitive_ts_types:
                continue

            parents = tuple(
                sorted({
                    self.primitive_ts_types[p] if p in self.primitive_ts_types else as_ts_class(p)
                    for p in data.parents
                    if p not in self.primitive_ts_types
                })
            )

            fields: list[FieldDef] = []
            for item in data.fields:
                types = tuple(
                    sorted({
                        self.primitive_ts_types[t] if t in self.primitive_ts_types else as_ts_class(t)
                        for t in item.types
                    })
                )
                fields.append(
                    FieldDef(
                        field=as_ts_field(item.field),
                        comment=item.comment,
                        types=types,
                    )
                )

            result[as_ts_class(class_name)] = ClassDef(
                comment=data.comment,
                parents=parents,
                fields=tuple(fields),
            )

        return result

    def jsdoc(self, lines: tuple[str, ...], indent: str = "") -> list[str]:
        lines = tuple([self.clean_comment(line) for line in lines if line.strip()])
        if not lines:
            return []
        return [f"{indent}/**", *[f"{indent} * {line}" for line in lines], f"{indent} */"]

    def ts_type(self, types: tuple[str, ...]) -> str:
        if not types:
            return "unknown[]"
        if len(types) == 1:
            return f"{types[0]}[]"
        return f"({ ' | '.join(types) })[]"

    def _generate(self, defs: dict[str, ClassDef]) -> str:
        defs = self.schema_to_ts_types(defs)

        result_lines = ["// THIS FILE IS AUTOMATICALLY GENERATED. DO NOT EDIT DIRECTLY."]

        for class_name in sorted(defs):
            data = defs[class_name]
            result_lines += self.jsdoc(data.comment)

            extends_part = f" extends {', '.join(data.parents)}" if data.parents else ""
            result_lines.append(f"export interface {class_name}{extends_part} {{")

            for field in data.fields:
                result_lines += self.jsdoc(field.comment, indent="  ")
                result_lines.append(f"  {field.field}?: {self.ts_type(field.types)};")

            result_lines.append("}\n")

        return "\n".join(result_lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ttl", default="https://schema.org/version/latest/schemaorg-current-https.ttl")
    parser.add_argument("--namespace", default="https://schema.org/")
    parser.add_argument("--out", default="-")
    parser.add_argument("--class", dest="classes", action="append", default=[])
    args = parser.parse_args()

    gen = TypescriptGenerator()
    result = gen.generate(args.classes, ttl_url=args.ttl, namespace_url=args.namespace)

    if args.out == "-":
        sys.stdout.write(result)
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(result)


if __name__ == "__main__":
    main()
