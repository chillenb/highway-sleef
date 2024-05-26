import argparse
import pathlib
import re
import sys
import textwrap

from treesitter_utils import *
# Tag Notes: di = int, df = float, du = unsigned

# Interface for translation:
# - Start with a tree-sitter function definition node
# - Translate function call expressions recursively
fn_call_translations = {}
fn_decl_translations = {}
fn_comments = {}
type_translations = {}
type_precisions = {}
constant_translations = {}
constant_types = {}
constant_comments = {}
macro_conditionals = {}
macro_conditional_translations = {}

source_file = {}
BUILTIN_TAG_NAMES = [b"df", b"di32", b"di", b"du32", b"du"]

def main():
    parser = argparse.ArgumentParser(
        description="Translate sleef source files into highway code"
    )
    parser.add_argument('sleef_src', help="Path of sleef 'src' folder")
    parser.add_argument('rename_data', help="Path of rename_data folder")
    parser.add_argument('output', help="Path to write generated header")
    
    args = parser.parse_args()

    sleef_src = pathlib.Path(args.sleef_src)
    rename_data = pathlib.Path(args.rename_data)
    out = open(args.output, 'w')


    target_functions = [
        # Single-precision ops
        "xexpf",
        "xexp2f",
        "xexp10f",
        "xexpm1f",
        "xlogf_u1",
        "xlogf",
        "xlog10f",
        "xlog2f",
        "xlog1pf",
        "xsqrtf_u05",
        "xsqrtf_u35",
        "xcbrtf",
        "xcbrtf_u1",
        "xhypotf_u05",
        "xhypotf_u35",
        "xpowf",
        "xsinf_u1",
        "xcosf_u1",
        "xtanf_u1",
        "xsinf",
        "xcosf",
        "xtanf",
        "xsinhf",
        "xcoshf",
        "xtanhf",
        "xsinhf_u35",
        "xcoshf_u35",
        "xtanhf_u35",
        "xacosf_u1",
        "xasinf_u1",
        "xasinhf",
        "xacosf",
        "xasinf",
        "xatanf",
        "xacoshf",
        "xatanf_u1",
        "xatanhf",
        # Double-precision ops
        "xexp",
        "xexp2",
        "xexp10",
        "xexpm1",
        "xlog_u1",
        "xlog",
        "xlog10",
        "xlog2",
        "xlog1p",
        "xsqrt_u05",
        "xsqrt_u35",
        "xcbrt",
        "xcbrt_u1",
        "xhypot_u05",
        "xhypot_u35",
        "xpow",
        "xsin_u1",
        "xcos_u1",
        "xtan_u1",
        "xsin",
        "xcos",
        "xtan",
        "xsinh",
        "xcosh",
        "xtanh",
        "xsinh_u35",
        "xcosh_u35",
        "xtanh_u35",
        "xacos_u1",
        "xasin_u1",
        "xasinh",
        "xacos",
        "xasin",
        "xatan",
        "xacosh",
        "xatan_u1",
        "xatanh",
    ]

    # Read data files to register translations for simd ops, intermediate functions, and types
    for old_name, new_name, comment in parse_tsv(rename_data / "function_renames.tsv", 3):
        key, translate_fn = function_rename_translator(old_name, new_name, old_name in target_functions)
        fn_call_translations[key] = translate_fn
        fn_decl_translations[key] = translate_fn
        fn_comments[key.decode()] = comment

    for in_spec, out_spec in parse_tsv(rename_data / "simd_ops.tsv", 2):
        key, translate_fn = simd_op_translator(in_spec, out_spec)
        fn_call_translations[key] = translate_fn
    
    for in_type, out_type, precision in parse_tsv(rename_data / "types.tsv", 3):
        key, translate_fn = type_rename_translator(in_type, out_type)
        type_translations[key] = translate_fn
        type_precisions[key] = precision

    for old_name, new_name, type, comment in parse_tsv(rename_data / "constant_renames.tsv", 4):
        key, translate_fn = constant_rename_translator(old_name, new_name)
        constant_translations[key] = translate_fn
        constant_types[key] = type.encode()
        constant_comments[key] = comment.encode()

    for in_condition, out_condition in parse_tsv(rename_data / "macro_conditionals.tsv", 2):
        macro_conditionals[in_condition] = out_condition
        key, translate_fn = macro_conditonal_translator(in_condition, out_condition)
        macro_conditional_translations[key] = translate_fn

    sources = [
        "libm/sleefsimdsp.c", 
        "libm/sleefsimddp.c", 
        "common/df.h", 
        "common/dd.h",
        "common/commonfuncs.h",
        "arch/helperneon32.h",
    ]
    calls = {}
    trees = {}

    function_nodes = collections.defaultdict(list) # name: [(node, exclude_bool)]
    for s in sources:
        text = open(sleef_src / s, "rb").read()
        
        tree = c_parse(text)
        trees[s] = tree
        
        calls = {**construct_callgraph(tree.root_node), **calls}
        for f in all_defined_functions(tree.root_node):
            source_file[f] = s

        for (n, _) in c_query("(function_definition) @fn_def").captures(tree.root_node):
            exclude = False

            # Check if n is under the wrong side of a conditional definition
            if n.parent.type in ["preproc_if", "preproc_ifdef", "preproc_elif"]:
                condition = n.parent.named_children[0].text.decode()
                if macro_conditionals.get(condition) == "0":
                    exclude = True
            if n.parent.type == "preproc_else":
                condition = n.parent.parent.named_children[0].text.decode()
                if macro_conditionals.get(condition) == "1":
                    exclude = True

            name = n.child_by_field_name("declarator").child_by_field_name("declarator").text.decode()
            function_nodes[name].append((n, exclude))

    fns_to_translate = []
    for t in target_functions:
        for f in topo_sort(t, calls):
            if (f in source_file and 
                f in fn_comments
                and f not in fns_to_translate
                and f not in target_functions
                ):
                fns_to_translate.append(f)
    fns_to_translate += target_functions

    helper_code = []
    code = []
    decls = []
    for f in fns_to_translate:
        if f == "xsqrtf_u35":
            pass #breakpoint()
        valid_nodes = [n for n, exclude in function_nodes[f] if not exclude]
        if len(valid_nodes) == 0:
            if len(function_nodes[f]) > 1:
                print(f"WARNING: found 0 valid definitions and {len(function_nodes[f])} invalid definitions for function {f} (using first invalid)", file=sys.stderr)
            node = function_nodes[f][0][0]
        else:
            node = valid_nodes[0]
        
        # Special-case for two function definitions withn a known #if ... #else ... #endif structure,
        # possibly with an intervening #elif that doesn't get used
        if len(valid_nodes) == 2 and \
            (valid_nodes[0].parent == valid_nodes[1].parent.parent or 
             valid_nodes[0].parent == valid_nodes[1].parent.parent.parent) and \
            valid_nodes[0].parent.named_children[0].text.decode() in macro_conditionals:
            translation_true = translate_sleef_function(f, valid_nodes[0])
            translation_false = translate_sleef_function(f, valid_nodes[1])
            body_true = translation_true[translation_true.find("{")+2:translation_true.rfind("}")]
            body_false = translation_false[translation_false.find("{")+2:translation_false.rfind("}")]
            translated_condition = macro_conditionals[valid_nodes[0].parent.named_children[0].text.decode()]
            maybe_newline = "" if body_false[-1] == "\n" else "\n"
            translation = translation_true.replace(
                body_true, 
                f"#if {translated_condition}\n{body_true}#else\n{body_false}{maybe_newline}#endif\n"
            )
        else:
            if len(valid_nodes) > 1:
                print(f"WARNING: found {len(valid_nodes)} valid definitions for function {f} (using first valid)", file=sys.stderr)
            translation = translate_sleef_function(f, node)


        if f in target_functions:
            code.append(translation)
            decls.append(translation[:translation.find("{")].strip() + ";")
        else:
            helper_code.append(translation)
    
    sleef_constant_defs = open(sleef_src / "common/misc.h", "rb").read()
    const_defs = translate_constant_defs(c_parse(sleef_constant_defs).root_node).decode()

    print(FILE_TEMPLATE.format(
        decls="\n\n".join(decls),
        const_defs=const_defs,
        helper_code="\n\n".join(helper_code),
        code="\n\n".join(code),
    ), file=out)

def translate_sleef_function(function_name, node):
    output_template = textwrap.dedent("""
    // {comment}
    // Translated from {file}:{line} {old_function_name}
    template<class D>
    HWY_INLINE {translated_function}
    """).strip()

    file = source_file[function_name]
    line = node.start_point[0] + 1
    [(decl, _)] = c_query("(function_definition declarator: (function_declarator) @decl)").captures(node)
    # Manually strip off some of the macros in the function definition
    start_pos = decl.start_byte - node.start_byte
    start_pos = node.text.rfind(b" ", 0, start_pos)
    start_pos = node.text.rfind(b" ", 0, start_pos)
    node = c_parse(node.text[start_pos+1:]).root_node.children[0]
    return output_template.format(
        comment = fn_comments[function_name],
        file = file,
        line = line,
        old_function_name = function_name,
        translated_function = translate_function(node).decode()
    )

def parse_tsv(path, count):
    for l in open(path):
        l = l.strip()
        if "#" in l:
            l = l[:l.find("#")]
        if len(l) == 0:
            continue
        res = l.split("\t")
        if len(res) != count:
            print(f"Error: got \"{res}\" with {len(res)} fields instead of {count}", file=sys.stderr)
        yield res

def simd_op_translator(in_spec, out_spec):
    in_spec = c_parse(in_spec.encode() + b";").root_node.children[0].children[0]
    out_spec = cpp_parse(out_spec.encode() + b";").root_node.children[0].children[0]
        
    c_args = c_query("(call_expression arguments: (argument_list (identifier) @arg))")
    in_args = [n.text for (n, _) in c_args.captures(in_spec)]

    c_fn_name = c_query("(call_expression function: (identifier) @fn_name)")
    (in_fn_name,) = [n.text for (n, _) in c_fn_name.captures(in_spec)]

    cpp_args = cpp_query("""
    (type_identifier) @arg
    (identifier) @arg
    """)

    tag_types = set()
    summary = []
    last_offset = 0
    for n, tag in cpp_args.captures(out_spec):
        summary.append(out_spec.text[last_offset:n.start_byte])
        if tag == "arg":
            if n.text in in_args:
                summary.append(in_args.index(n.text))
            else:
                summary.append(n.text)
                if n.text in BUILTIN_TAG_NAMES:
                    tag_types.add(n.text)
        last_offset = n.end_byte
    summary.append(out_spec.text[last_offset:])

    def translate(node, ctx):
        assert node.type == "call_expression" 
        assert node.child_by_field_name("function").text == in_fn_name
        arg_nodes = node.child_by_field_name("arguments").named_children
        ctx["tag_types"] |= tag_types
        return b"".join(
            translate_tree(arg_nodes[i], ctx) if type(i) is int else i
            for i in summary
        )
    return (in_fn_name, translate)

def function_rename_translator(old_name, new_name, is_top_level=False):
    old_name = old_name.encode()
    new_name = new_name.encode()
    def translate(node, ctx):
        assert node.type == "call_expression" or node.type == "function_declarator"
        if node.type == "call_expression":
            assert node.child_by_field_name("function").text == old_name
            # Pass tag as first parameter, and mark that we need the tag
            ctx["tag_types"].add(b"df")
            ret = new_name + b"(df, " + translate_tree(node.child_by_field_name("arguments"), ctx).lstrip(b"(")
            if is_top_level:
                return b"sleef::" + ret
            else:
                return ret
        if node.type == "function_declarator":
            assert node.child_by_field_name("declarator").text == old_name
            # Add in tag as first parameter
            return new_name + b"(const D df, " + translate_tree(node.child_by_field_name("parameters"), ctx).lstrip(b"(")

    return (old_name, translate)

def translate_fn_call(node, ctx):
    assert node.type == "call_expression" 
    fn_name = node.child_by_field_name("function").text
    if fn_name in fn_call_translations:
        return fn_call_translations[fn_name](node, ctx)
    else:
        return None

def translate_fn_decl(node, ctx):
    assert node.type == "function_declarator"
    fn_name = node.child_by_field_name("declarator").text
    if fn_name in fn_decl_translations:
        return fn_decl_translations[fn_name](node, ctx)
    else:
        return None

def type_rename_translator(old_name, new_name):
    old_name = old_name.encode()
    new_name = new_name.encode()
    def translate(node, ctx):
        assert node.type == "type_identifier"
        assert node.text == old_name
        return new_name
    return (old_name, translate)

def translate_type_id(node, ctx):
    assert node.type == "type_identifier"
    if node.text in type_translations:
        return type_translations[node.text](node, ctx)
    else:
        return None
    
def constant_rename_translator(old_name, new_name):
    old_name = old_name.encode()
    new_name = new_name.encode()
    def translate(node, ctx):
        assert node.type == "identifier"
        assert node.text == old_name
        return new_name
    return (old_name, translate)

def translate_constant(node, ctx):
    assert node.type == "identifier"
    if node.text in constant_translations:
        return constant_translations[node.text](node, ctx)
    else:
        return None

def macro_conditonal_translator(in_condition, out_condition):
    in_condition = in_condition.encode()
    out_condition = out_condition.encode()
    def translate(node, ctx):
        assert node.type in ["preproc_if", "preproc_ifdef"]
        # Verify assumptions about the early nodes in the children
        if node.type == "preproc_if":
            assert node.children[0].text == b"#if"    
            assert node.field_name_for_child(1) == "condition"
            assert node.children[2].text == b"\n"
            first_line_nodes = 3
        else:
            assert node.children[0].text == b"#ifdef"
            assert node.field_name_for_child(1) == "name"
            assert node.children[2].is_named
            first_line_nodes = 2
        

        if out_condition == b"0":
            alt = node.child_by_field_name("alternative")
            if alt is None:
                return b""
            alt = translate_tree(alt, ctx)
            return alt[alt.find(b"\n")+1:]
        
        last_byte = node.start_byte
        child_text = []
        for c in node.children:
            child_text.append(ctx["text"][last_byte:c.start_byte])
            last_byte=c.end_byte
            child_text.append(translate_tree(c, ctx))
        child_text.append(ctx["text"][last_byte:node.end_byte])
        
        if out_condition == b"1":
            [alt_index] = [i for i, _  in enumerate(node.children) if node.field_name_for_child(i) == "alternative"]
            # child_text is [literal, child_0, literal, child_1, ... literal, child_n, literal]
            child_text = child_text[(1 + 2*first_line_nodes):(1 + 2*alt_index)]
            return b"".join(child_text)
        else:
            # Condition text should be position 3
            child_text[3] = out_condition
        return b"".join(child_text)

    return (in_condition, translate)

def translate_macro_conditional(node, ctx):
    assert node.type in ["preproc_if", "preproc_ifdef"]
    condition = node.named_children[0].text
    if condition in macro_conditional_translations:
        return macro_conditional_translations[condition](node, ctx)
    else:
        return None

def translate_tag_name_conflict(node, ctx):
    assert node.type == "identifier"
    if node.text in BUILTIN_TAG_NAMES:
        return node.text + b"_"
    return node.text

def ancestors(node):
    while node.parent is not None:
        node = node.parent
        yield node

def translate_function(node):
    assert node.type == "function_definition"

    root_node = list(ancestors(node))[-1]

    handlers = {
        "fn_call": translate_fn_call,
        "type_id": translate_type_id,
        "fn_decl": translate_fn_decl,
        "const_id": translate_constant,
        "macro_conditional": translate_macro_conditional,
        "tag_name_conflict": translate_tag_name_conflict,
    }

    captures = c_query(
    """
    (call_expression) @fn_call
    (type_identifier) @type_id
    (function_declarator) @fn_decl
    (identifier) @const_id
    (preproc_if) @macro_conditional
    (preproc_ifdef) @macro_conditional
    ((identifier) @tag_name_conflict
        (#match? @tag_name_conflict "^d[iu](32)?$"))
    """
    ).captures(node)
    fn_body = node.child_by_field_name("body")
    
    declared_identifiers = set(
        n.text for n, _ in c_query("(_ declarator: (identifier) @id)").captures(node)
    )
    declared_identifiers |= set(fn_call_translations.keys())
    
    unknown_ids = set()
    for n, _ in c_query("(identifier) @id").captures(fn_body):
        if n.text in declared_identifiers:
            continue
        if n.text in fn_call_translations:
            continue
        if n.text in constant_translations:
            continue
        if n.text.decode() in macro_conditionals:
            continue
        
        is_known_macro = False
        prev_a = n
        for a in ancestors(n):
            if a.type in ["preproc_if", "preproc_ifdef"]:
                is_known_macro = a.named_children[0].text in macro_conditional_translations and prev_a == a.named_children[0]
                break    
            prev_a = a
        if is_known_macro:
            continue

        unknown_ids.add(n.text)

    
    if len(unknown_ids) > 0:
        name = node.child_by_field_name("declarator").child_by_field_name("declarator").text.decode()
        print(f"WARNING: Possibly unknown identifiers in {name}: ", b", ".join(unknown_ids).decode(), file=sys.stderr)

    ctx = {
        "text": root_node.text,
        "capture_handler": {n.id: handlers[t] for (n, t) in captures},
        "has_child_match": set(n.id for c in captures for n in ancestors(c[0])),
        "tag_types": set(),
    }

    # Determine if function should be restricted to floats or doubles:
    # 1. If return type is float/double, use that
    # 2. If return type is ambiguous, use type of first float/double argument
    return_type = translate_tree(node.child_by_field_name("type"), ctx).decode()
    type_nodes = [node.child_by_field_name("type")] + \
        [n for n, _ in c_query("(parameter_declaration (type_identifier) @type)").captures(node.child_by_field_name("declarator"))]
    for n in type_nodes:
        if "float" == type_precisions[n.text]:
            return_type = f"HWY_SLEEF_IF_FLOAT(D, {return_type})".encode()
            break
        elif "double" == type_precisions[n.text]:
            return_type = f"HWY_SLEEF_IF_DOUBLE(D, {return_type})".encode()
            break
    if not return_type.startswith(b"HWY_SLEEF_IF"):
        print(f"WARNING: Could not determine return type precision for function: {node.child_by_field_name('declarator').text.decode()}")

    signature = translate_tree(node.child_by_field_name("declarator"), ctx)
    body = translate_tree(node.child_by_field_name("body"), ctx)
    
    tag_defs = []
    for tag in ctx["tag_types"]:
        if tag == b"df":
            continue # Coming in via parameters
        elif tag == b"di":
            tag_defs.append(b"  RebindToSigned<D> di;")
        elif tag == b"di32":
            tag_defs.append(b"  RebindToSigned32<D> di32;")
        elif tag == b"du":
            tag_defs.append(b"  RebindToUnsigned<D> du;")
        elif tag == b"du32":
            tag_defs.append(b"  RebindToUnsigned32<D> du32;")
        else:
            assert False
    tag_defs = b"\n".join(tag_defs)
    if len(tag_defs) > 0:
        tag_defs += b"\n  \n"
    body = b"{\n" + tag_defs + body.lstrip(b"{\n")
    
    return b" ".join([return_type, signature, body])

def translate_tree(node, ctx):
    # If the current node is a match, call the appropriate handler
    if node.id in ctx["capture_handler"]:
        res = ctx["capture_handler"][node.id](node, ctx)
        if res is not None:
            return res

    if node.id not in ctx["has_child_match"]:
        return node.text

    # Recurse into children, preserving all text outside of the child nodes themselves
    last_byte = node.start_byte
    child_text = []
    for c in node.children:
        child_text.append(ctx["text"][last_byte:c.start_byte])
        last_byte=c.end_byte
        child_text.append(translate_tree(c, ctx))
    child_text.append(ctx["text"][last_byte:node.end_byte])
    return b"".join(child_text)


def translate_preproc_define(node, ctx):
    assert node.type == "preproc_def"
    name = node.child_by_field_name("name")
    value = node.child_by_field_name("value")
    # breakpoint()
    if name.text in constant_translations:
        value = translate_tree(value, ctx)
        if b"//" in value:
            value = value[:value.find(b"//")]
        return (
            b" ".join([b"constexpr", constant_types[name.text], translate_tree(name, ctx), b"=", value + b";"]) +
            b" // " + constant_comments[name.text] + b"\n" 
        )
    else:
        return b""

def translate_preproc_if(node, ctx):
    assert node.type == "preproc_if"
    children = [n for n in node.children if n.type=="preproc_def"]
    # Note: we ignore the "alternative" branch in #if #else, since if 
    # nothing is defined under the true case, we can probably ignore the false case

    # Cut out if there aren't any useful defines below it
    if all(len(translate_tree(c, ctx)) == 0 for c in children):
        return b""

    # Otherwise, defer to normal translation setup
    return None
        
           

def translate_constant_defs(root_node):
    """Process constant macro definitions and return a translated copy of the code"""
    handlers = {
        "define": translate_preproc_define,
        "const_id": translate_constant,
        "preproc_if": translate_preproc_if,
        "preproc_strip": lambda n, ctx: b"",
    }

    captures = c_query(
    """
    (preproc_def) @define
    (identifier) @const_id
    (preproc_if) @preproc_if
    (preproc_function_def) @preproc_strip
    (preproc_ifdef) @preproc_strip

    (preproc_if (comment) @preproc_strip)
    (preproc_elif (comment) @preproc_strip)
    """
    ).captures(root_node)

    ctx = {
        "text": root_node.text,
        "capture_handler": {n.id: handlers[t] for (n, t) in captures},
        "has_child_match": set(n.id for c in captures for n in ancestors(c[0])),
        
    }

    query_top_level = c_query(
    """
    (translation_unit (preproc_ifdef (preproc_if) @if))
    (translation_unit (preproc_ifdef (preproc_def) @define))
    (translation_unit (preproc_ifdef (preproc_ifdef (preproc_def) @define)))
    """
    )
    translations = []
    for n, tag in query_top_level.captures(root_node):
        if tag == "define":
            translations.append(translate_tree(n, ctx).strip())
        if tag == "if":
            res = translate_tree(n, ctx)
            res = re.sub(b'\n[\n ]+', b'\n', res)
            translations.append(res)
    translations = [t for t in translations if len(t) > 0]
    return b"\n".join(translations)


def topo_sort(start, callgraph):
    """Return a topologically-sorted list from a callgraph (inefficient implementation)"""
    if start not in callgraph:
        return [start]
    
    res = []
    for child in callgraph[start]:
        res += [x for x in topo_sort(child, callgraph) if x not in res]
    
    res.append(start)
    return res

FILE_TEMPLATE = textwrap.dedent(
"""
// This file is translated from the SLEEF vectorized math library.
// Translation performed by Ben Parks copyright 2024.
// Translated elements available under the following licenses, at your option:
//   BSL-1.0 (http://www.boost.org/LICENSE_1_0.txt),
//   MIT (https://opensource.org/license/MIT/), and
//   Apache-2.0 (https://www.apache.org/licenses/LICENSE-2.0)
// 
// Original SLEEF copyright:
//   Copyright Naoki Shibata and contributors 2010 - 2021.
// Distributed under the Boost Software License, Version 1.0.
//    (See accompanying file LICENSE.txt or copy at
//          http://www.boost.org/LICENSE_1_0.txt)

#if defined(HIGHWAY_HWY_CONTRIB_SLEEF_SLEEF_INL_) == \\
    defined(HWY_TARGET_TOGGLE)  // NOLINT
#ifdef HIGHWAY_HWY_CONTRIB_SLEEF_SLEEF_INL_
#undef HIGHWAY_HWY_CONTRIB_SLEEF_SLEEF_INL_
#else
#define HIGHWAY_HWY_CONTRIB_SLEEF_SLEEF_INL_
#endif

#include <type_traits>
#include "hwy/highway.h"

extern const float PayneHanekReductionTable_float[]; // Precomputed table of exponent values for Payne Hanek reduction
extern const double PayneHanekReductionTable_double[]; // Precomputed table of exponent values for Payne Hanek reduction

HWY_BEFORE_NAMESPACE();
namespace hwy {{
namespace HWY_NAMESPACE {{

#if HWY_ARCH_X86 && HWY_TARGET <= HWY_AVX3
HWY_API Vec512<float> GetExponent(Vec512<float> x) {{
  return Vec512<float>{{_mm512_getexp_ps(x.raw)}};
}}
HWY_API Vec256<float> GetExponent(Vec256<float> x) {{
  return Vec256<float>{{_mm256_getexp_ps(x.raw)}};
}}
template<size_t N>
HWY_API Vec128<float, N> GetExponent(Vec128<float, N> x) {{
  return Vec128<float, N>{{_mm_getexp_ps(x.raw)}};
}}

HWY_API Vec512<double> GetExponent(Vec512<double> x) {{
  return Vec512<double>{{_mm512_getexp_pd(x.raw)}};
}}
HWY_API Vec256<double> GetExponent(Vec256<double> x) {{
  return Vec256<double>{{_mm256_getexp_pd(x.raw)}};
}}
template<size_t N>
HWY_API Vec128<double, N> GetExponent(Vec128<double, N> x) {{
  return Vec128<double, N>{{_mm_getexp_pd(x.raw)}};
}}

HWY_API Vec512<float> GetMantissa(Vec512<float> x) {{
  return Vec512<float>{{_mm512_getmant_ps(x.raw,  _MM_MANT_NORM_p75_1p5, _MM_MANT_SIGN_nan)}};
}}
HWY_API Vec256<float> GetMantissa(Vec256<float> x) {{
  return Vec256<float>{{_mm256_getmant_ps(x.raw,  _MM_MANT_NORM_p75_1p5, _MM_MANT_SIGN_nan)}};
}}
template<size_t N>
HWY_API Vec128<float, N> GetMantissa(Vec128<float, N> x) {{
  return Vec128<float, N>{{_mm_getmant_ps(x.raw,  _MM_MANT_NORM_p75_1p5, _MM_MANT_SIGN_nan)}};
}}

HWY_API Vec512<double> GetMantissa(Vec512<double> x) {{
  return Vec512<double>{{_mm512_getmant_pd(x.raw,  _MM_MANT_NORM_p75_1p5, _MM_MANT_SIGN_nan)}};
}}
HWY_API Vec256<double> GetMantissa(Vec256<double> x) {{
  return Vec256<double>{{_mm256_getmant_pd(x.raw,  _MM_MANT_NORM_p75_1p5, _MM_MANT_SIGN_nan)}};
}}
template<size_t N>
HWY_API Vec128<double, N> GetMantissa(Vec128<double, N> x) {{
  return Vec128<double, N>{{_mm_getmant_pd(x.raw,  _MM_MANT_NORM_p75_1p5, _MM_MANT_SIGN_nan)}};
}}

template<int I>
HWY_API Vec512<float> Fixup(Vec512<float> a, Vec512<float> b, Vec512<int> c) {{
    return Vec512<float>{{_mm512_fixupimm_ps(a.raw, b.raw, c.raw, I)}};
}}
template<int I>
HWY_API Vec256<float> Fixup(Vec256<float> a, Vec256<float> b, Vec256<int> c) {{
    return Vec256<float>{{_mm256_fixupimm_ps(a.raw, b.raw, c.raw, I)}};
}}
template<int I, size_t N>
HWY_API Vec128<float, N> Fixup(Vec128<float, N> a, Vec128<float, N> b, Vec128<int, N> c) {{
    return Vec128<float, N>{{_mm_fixupimm_ps(a.raw, b.raw, c.raw, I)}};
}}

template<int I>
HWY_API Vec512<double> Fixup(Vec512<double> a, Vec512<double> b, Vec512<int64_t> c) {{
    return Vec512<double>{{_mm512_fixupimm_pd(a.raw, b.raw, c.raw, I)}};
}}
template<int I>
HWY_API Vec256<double> Fixup(Vec256<double> a, Vec256<double> b, Vec256<int64_t> c) {{
    return Vec256<double>{{_mm256_fixupimm_pd(a.raw, b.raw, c.raw, I)}};
}}
template<int I, size_t N>
HWY_API Vec128<double, N> Fixup(Vec128<double, N> a, Vec128<double, N> b, Vec128<int64_t, N> c) {{
    return Vec128<double, N>{{_mm_fixupimm_pd(a.raw, b.raw, c.raw, I)}};
}}
#endif

namespace sleef {{

#undef HWY_SLEEF_HAS_FMA
#if (HWY_ARCH_X86 && HWY_TARGET < HWY_SSE4) || HWY_ARCH_ARM || HWY_ARCH_S390X || HWY_ARCH_RVV 
#define HWY_SLEEF_HAS_FMA 1
#endif

#undef HWY_SLEEF_IF_DOUBLE
#define HWY_SLEEF_IF_DOUBLE(D, V) typename std::enable_if<std::is_same<double, TFromD<D>>::value, V>::type
#undef HWY_SLEEF_IF_FLOAT
#define HWY_SLEEF_IF_FLOAT(D, V) typename std::enable_if<std::is_same<float, TFromD<D>>::value, V>::type

{decls}

namespace {{

template<class D>
using RebindToSigned32 = Rebind<int32_t, D>;
template<class D>
using RebindToUnsigned32 = Rebind<uint32_t, D>;

// Estrin's Scheme is a faster method for evaluating large polynomials on
// super scalar architectures. It works by factoring the Horner's Method
// polynomial into power of two sub-trees that can be evaluated in parallel.
// Wikipedia Link: https://en.wikipedia.org/wiki/Estrin%27s_scheme
template <class T>
HWY_INLINE HWY_MAYBE_UNUSED T Estrin(T x, T c0, T c1) {{
  return MulAdd(c1, x, c0);
}}
template <class T>
HWY_INLINE HWY_MAYBE_UNUSED T Estrin(T x, T x2, T c0, T c1, T c2) {{
  return MulAdd(x2, c2, MulAdd(c1, x, c0));
}}
template <class T>
HWY_INLINE HWY_MAYBE_UNUSED T Estrin(T x, T x2, T c0, T c1, T c2, T c3) {{
  return MulAdd(x2, MulAdd(c3, x, c2), MulAdd(c1, x, c0));
}}
template <class T>
HWY_INLINE HWY_MAYBE_UNUSED T Estrin(T x, T x2, T x4, T c0, T c1, T c2, T c3, T c4) {{
  return MulAdd(x4, c4, MulAdd(x2, MulAdd(c3, x, c2), MulAdd(c1, x, c0)));
}}
template <class T>
HWY_INLINE HWY_MAYBE_UNUSED T Estrin(T x, T x2, T x4, T c0, T c1, T c2, T c3, T c4, T c5) {{
  return MulAdd(x4, MulAdd(c5, x, c4),
                MulAdd(x2, MulAdd(c3, x, c2), MulAdd(c1, x, c0)));
}}
template <class T>
HWY_INLINE HWY_MAYBE_UNUSED T Estrin(T x, T x2, T x4, T c0, T c1, T c2, T c3, T c4, T c5,
                                     T c6) {{
  return MulAdd(x4, MulAdd(x2, c6, MulAdd(c5, x, c4)),
                MulAdd(x2, MulAdd(c3, x, c2), MulAdd(c1, x, c0)));
}}
template <class T>
HWY_INLINE HWY_MAYBE_UNUSED T Estrin(T x, T x2, T x4, T c0, T c1, T c2, T c3, T c4, T c5,
                                     T c6, T c7) {{
  return MulAdd(x4, MulAdd(x2, MulAdd(c7, x, c6), MulAdd(c5, x, c4)),
                MulAdd(x2, MulAdd(c3, x, c2), MulAdd(c1, x, c0)));
}}
template <class T>
HWY_INLINE HWY_MAYBE_UNUSED T Estrin(T x, T x2, T x4, T x8, T c0, T c1, T c2, T c3, T c4, T c5,
                                     T c6, T c7, T c8) {{
  return MulAdd(x8, c8,
                MulAdd(x4, MulAdd(x2, MulAdd(c7, x, c6), MulAdd(c5, x, c4)),
                       MulAdd(x2, MulAdd(c3, x, c2), MulAdd(c1, x, c0))));
}}
template <class T>
HWY_INLINE HWY_MAYBE_UNUSED T Estrin(T x, T x2, T x4, T x8, T c0, T c1, T c2, T c3, T c4, T c5,
                                     T c6, T c7, T c8, T c9) {{
  return MulAdd(x8, MulAdd(c9, x, c8),
                MulAdd(x4, MulAdd(x2, MulAdd(c7, x, c6), MulAdd(c5, x, c4)),
                       MulAdd(x2, MulAdd(c3, x, c2), MulAdd(c1, x, c0))));
}}
template <class T>
HWY_INLINE HWY_MAYBE_UNUSED T Estrin(T x, T x2, T x4, T x8, T c0, T c1, T c2, T c3, T c4, T c5,
                                     T c6, T c7, T c8, T c9, T c10) {{
  return MulAdd(x8, MulAdd(x2, c10, MulAdd(c9, x, c8)),
                MulAdd(x4, MulAdd(x2, MulAdd(c7, x, c6), MulAdd(c5, x, c4)),
                       MulAdd(x2, MulAdd(c3, x, c2), MulAdd(c1, x, c0))));
}}
template <class T>
HWY_INLINE HWY_MAYBE_UNUSED T Estrin(T x, T x2, T x4, T x8, T c0, T c1, T c2, T c3, T c4, T c5,
                                     T c6, T c7, T c8, T c9, T c10, T c11) {{
  return MulAdd(x8, MulAdd(x2, MulAdd(c11, x, c10), MulAdd(c9, x, c8)),
                MulAdd(x4, MulAdd(x2, MulAdd(c7, x, c6), MulAdd(c5, x, c4)),
                       MulAdd(x2, MulAdd(c3, x, c2), MulAdd(c1, x, c0))));
}}
template <class T>
HWY_INLINE HWY_MAYBE_UNUSED T Estrin(T x, T x2, T x4, T x8, T c0, T c1, T c2, T c3, T c4, T c5,
                                     T c6, T c7, T c8, T c9, T c10, T c11,
                                     T c12) {{
  return MulAdd(
      x8, MulAdd(x4, c12, MulAdd(x2, MulAdd(c11, x, c10), MulAdd(c9, x, c8))),
      MulAdd(x4, MulAdd(x2, MulAdd(c7, x, c6), MulAdd(c5, x, c4)),
             MulAdd(x2, MulAdd(c3, x, c2), MulAdd(c1, x, c0))));
}}
template <class T>
HWY_INLINE HWY_MAYBE_UNUSED T Estrin(T x, T x2, T x4, T x8, T c0, T c1, T c2, T c3, T c4, T c5,
                                     T c6, T c7, T c8, T c9, T c10, T c11,
                                     T c12, T c13) {{
  return MulAdd(x8,
                MulAdd(x4, MulAdd(c13, x, c12),
                       MulAdd(x2, MulAdd(c11, x, c10), MulAdd(c9, x, c8))),
                MulAdd(x4, MulAdd(x2, MulAdd(c7, x, c6), MulAdd(c5, x, c4)),
                       MulAdd(x2, MulAdd(c3, x, c2), MulAdd(c1, x, c0))));
}}
template <class T>
HWY_INLINE HWY_MAYBE_UNUSED T Estrin(T x, T x2, T x4, T x8, T c0, T c1, T c2, T c3, T c4, T c5,
                                     T c6, T c7, T c8, T c9, T c10, T c11,
                                     T c12, T c13, T c14) {{
  return MulAdd(x8,
                MulAdd(x4, MulAdd(x2, c14, MulAdd(c13, x, c12)),
                       MulAdd(x2, MulAdd(c11, x, c10), MulAdd(c9, x, c8))),
                MulAdd(x4, MulAdd(x2, MulAdd(c7, x, c6), MulAdd(c5, x, c4)),
                       MulAdd(x2, MulAdd(c3, x, c2), MulAdd(c1, x, c0))));
}}
template <class T>
HWY_INLINE HWY_MAYBE_UNUSED T Estrin(T x, T x2, T x4, T x8, T c0, T c1, T c2, T c3, T c4, T c5,
                                     T c6, T c7, T c8, T c9, T c10, T c11,
                                     T c12, T c13, T c14, T c15) {{
  return MulAdd(x8,
                MulAdd(x4, MulAdd(x2, MulAdd(c15, x, c14), MulAdd(c13, x, c12)),
                       MulAdd(x2, MulAdd(c11, x, c10), MulAdd(c9, x, c8))),
                MulAdd(x4, MulAdd(x2, MulAdd(c7, x, c6), MulAdd(c5, x, c4)),
                       MulAdd(x2, MulAdd(c3, x, c2), MulAdd(c1, x, c0))));
}}
template <class T>
HWY_INLINE HWY_MAYBE_UNUSED T Estrin(T x, T x2, T x4, T x8, T x16, T c0, T c1, T c2, T c3, T c4, T c5,
                                     T c6, T c7, T c8, T c9, T c10, T c11,
                                     T c12, T c13, T c14, T c15, T c16) {{
  return MulAdd(
      x16, c16,
      MulAdd(x8,
             MulAdd(x4, MulAdd(x2, MulAdd(c15, x, c14), MulAdd(c13, x, c12)),
                    MulAdd(x2, MulAdd(c11, x, c10), MulAdd(c9, x, c8))),
             MulAdd(x4, MulAdd(x2, MulAdd(c7, x, c6), MulAdd(c5, x, c4)),
                    MulAdd(x2, MulAdd(c3, x, c2), MulAdd(c1, x, c0)))));
}}
template <class T>
HWY_INLINE HWY_MAYBE_UNUSED T Estrin(T x, T x2, T x4, T x8, T x16, T c0, T c1, T c2, T c3, T c4, T c5,
                                     T c6, T c7, T c8, T c9, T c10, T c11,
                                     T c12, T c13, T c14, T c15, T c16, T c17) {{
  return MulAdd(
      x16, MulAdd(c17, x, c16),
      MulAdd(x8,
             MulAdd(x4, MulAdd(x2, MulAdd(c15, x, c14), MulAdd(c13, x, c12)),
                    MulAdd(x2, MulAdd(c11, x, c10), MulAdd(c9, x, c8))),
             MulAdd(x4, MulAdd(x2, MulAdd(c7, x, c6), MulAdd(c5, x, c4)),
                    MulAdd(x2, MulAdd(c3, x, c2), MulAdd(c1, x, c0)))));
}}
template <class T>
HWY_INLINE HWY_MAYBE_UNUSED T Estrin(T x, T x2, T x4, T x8, T x16, T c0, T c1, T c2, T c3, T c4, T c5,
                                     T c6, T c7, T c8, T c9, T c10, T c11,
                                     T c12, T c13, T c14, T c15, T c16, T c17,
                                     T c18) {{
  return MulAdd(
      x16, MulAdd(x2, c18, MulAdd(c17, x, c16)),
      MulAdd(x8,
             MulAdd(x4, MulAdd(x2, MulAdd(c15, x, c14), MulAdd(c13, x, c12)),
                    MulAdd(x2, MulAdd(c11, x, c10), MulAdd(c9, x, c8))),
             MulAdd(x4, MulAdd(x2, MulAdd(c7, x, c6), MulAdd(c5, x, c4)),
                    MulAdd(x2, MulAdd(c3, x, c2), MulAdd(c1, x, c0)))));
}}

//////////////////
// Constants
//////////////////
{const_defs}


{helper_code}

}}

{code}

}}  // namespace sleef
}}  // namespace HWY_NAMESPACE
}}  // namespace hwy
HWY_AFTER_NAMESPACE();

#endif  // HIGHWAY_HWY_CONTRIB_SLEEF_SLEEF_INL_

#if HWY_ONCE
__attribute__((aligned(64)))
const double PayneHanekReductionTable_double[] = {{
    // clang-format off
  0.15915494309189531785, 1.7916237278037667488e-17, 2.5454160968749269937e-33, 2.1132476107887107169e-49,
  0.03415494309189533173, 4.0384494702232122736e-18, 1.0046721413651383112e-33, 2.1132476107887107169e-49,
  0.03415494309189533173, 4.0384494702232122736e-18, 1.0046721413651383112e-33, 2.1132476107887107169e-49,
  0.0029049430918953351999, 5.6900251826959904774e-19, 4.1707169171520598517e-35, -2.496415728504571394e-51,
  0.0029049430918953351999, 5.6900251826959904774e-19, 4.1707169171520598517e-35, -2.496415728504571394e-51,
  0.0029049430918953351999, 5.6900251826959904774e-19, 4.1707169171520598517e-35, -2.496415728504571394e-51,
  0.0029049430918953351999, 5.6900251826959904774e-19, 4.1707169171520598517e-35, -2.496415728504571394e-51,
  0.00095181809189533563356, 1.3532164927539732229e-19, -6.4410794381603004826e-36, 1.7634898158762436344e-52,
  0.00095181809189533563356, 1.3532164927539732229e-19, -6.4410794381603004826e-36, 1.7634898158762436344e-52,
  0.00046353684189533574198, 2.6901432026846872871e-20, -4.2254836195018827479e-37, 9.301187206862134399e-54,
  0.00021939621689533574198, 2.6901432026846872871e-20, -4.2254836195018827479e-37, 9.301187206862134399e-54,
  9.7325904395335769087e-05, -2.0362228529073840241e-22, 6.2960434583523738135e-40, 2.6283399642369025999e-57,
  3.6290748145335769087e-05, -2.0362228529073840241e-22, 6.2960434583523738135e-40, 2.6283399642369025999e-57,
  5.7731700203357690874e-06, -2.0362228529073840241e-22, 6.2960434583523738135e-40, 2.6283399642369025999e-57,
  5.7731700203357690874e-06, -2.0362228529073840241e-22, 6.2960434583523738135e-40, 2.6283399642369025999e-57,
  5.7731700203357690874e-06, -2.0362228529073840241e-22, 6.2960434583523738135e-40, 2.6283399642369025999e-57,
  1.9584727547107690874e-06, -2.0362228529073840241e-22, 6.2960434583523738135e-40, 2.6283399642369025999e-57,
  5.1124121898268875627e-08, 8.135951522836682362e-24, 6.2960434583523738135e-40, 2.6283399642369025999e-57,
  5.1124121898268875627e-08, 8.135951522836682362e-24, 6.2960434583523738135e-40, 2.6283399642369025999e-57,
  5.1124121898268875627e-08, 8.135951522836682362e-24, 6.2960434583523738135e-40, 2.6283399642369025999e-57,
  5.1124121898268875627e-08, 8.135951522836682362e-24, 6.2960434583523738135e-40, 2.6283399642369025999e-57,
  5.1124121898268875627e-08, 8.135951522836682362e-24, 6.2960434583523738135e-40, 2.6283399642369025999e-57,
  5.1124121898268875627e-08, 8.135951522836682362e-24, 6.2960434583523738135e-40, 2.6283399642369025999e-57,
  2.1321799510573569745e-08, 1.5185066224124613304e-24, 2.6226236120327253511e-40, 2.6283399642369025999e-57,
  6.4206383167259151492e-09, -1.3585460269359374382e-25, -1.3244127270701094468e-41, -2.4695541513869446866e-57,
  6.4206383167259151492e-09, -1.3585460269359374382e-25, -1.3244127270701094468e-41, -2.4695541513869446866e-57,
  2.6953480182640010867e-09, -1.3585460269359374382e-25, -1.3244127270701094468e-41, -2.4695541513869446866e-57,
  8.3270286903304384868e-10, 7.0940550444663151936e-26, 9.7147467687967058732e-42, 7.9392906424978921242e-59,
  8.3270286903304384868e-10, 7.0940550444663151936e-26, 9.7147467687967058732e-42, 7.9392906424978921242e-59,
  3.6704158172530459087e-10, 7.0940550444663151936e-26, 9.7147467687967058732e-42, 7.9392906424978921242e-59,
  1.3421093807143501366e-10, 1.9241762160098927996e-26, 3.9750282589222551507e-42, 7.9392906424978921242e-59,
  1.7795616244500218596e-11, -1.452834466126541428e-28, -1.5869767474823787636e-44, -2.6168913164368963837e-61,
  1.7795616244500218596e-11, -1.452834466126541428e-28, -1.5869767474823787636e-44, -2.6168913164368963837e-61,
  1.7795616244500218596e-11, -1.452834466126541428e-28, -1.5869767474823787636e-44, -2.6168913164368963837e-61,
  3.2437010161333667893e-12, -1.452834466126541428e-28, -1.5869767474823787636e-44, -2.6168913164368963837e-61,
  3.2437010161333667893e-12, -1.452834466126541428e-28, -1.5869767474823787636e-44, -2.6168913164368963837e-61,
  3.2437010161333667893e-12, -1.452834466126541428e-28, -1.5869767474823787636e-44, -2.6168913164368963837e-61,
  1.4247116125875099096e-12, 2.5861333686050385673e-28, 2.8971783383570358633e-44, -2.6168913164368963837e-61,
  5.1521691081458187359e-13, 5.6664945123924856962e-29, 6.5510079543732854985e-45, -2.6168913164368963837e-61,
  6.0469559928117805118e-14, 6.1778471897801070206e-30, 9.4581409707401690366e-46, 4.9461632249367446986e-62,
  6.0469559928117805118e-14, 6.1778471897801070206e-30, 9.4581409707401690366e-46, 4.9461632249367446986e-62,
  6.0469559928117805118e-14, 6.1778471897801070206e-30, 9.4581409707401690366e-46, 4.9461632249367446986e-62,
  3.6261410673097965595e-15, -1.3304005198798645927e-31, -1.7578597149294783985e-47, 8.4432539107728104262e-64,
  3.6261410673097965595e-15, -1.3304005198798645927e-31, -1.7578597149294783985e-47, 8.4432539107728104262e-64,
  3.6261410673097965595e-15, -1.3304005198798645927e-31, -1.7578597149294783985e-47, 8.4432539107728104262e-64,
  3.6261410673097965595e-15, -1.3304005198798645927e-31, -1.7578597149294783985e-47, 8.4432539107728104262e-64,
  7.3427388509295482183e-17, 1.4871367740953237822e-32, -1.1571307704883330232e-48, -6.7249112515659578102e-65,
  7.3427388509295482183e-17, 1.4871367740953237822e-32, -1.1571307704883330232e-48, -6.7249112515659578102e-65,
  7.3427388509295482183e-17, 1.4871367740953237822e-32, -1.1571307704883330232e-48, -6.7249112515659578102e-65,
  7.3427388509295482183e-17, 1.4871367740953237822e-32, -1.1571307704883330232e-48, -6.7249112515659578102e-65,
  7.3427388509295482183e-17, 1.4871367740953237822e-32, -1.1571307704883330232e-48, -6.7249112515659578102e-65,
  7.3427388509295482183e-17, 1.4871367740953237822e-32, -1.1571307704883330232e-48, -6.7249112515659578102e-65,
  1.7916237278037667488e-17, 2.5454160968749269937e-33, 2.1132476107887107169e-49, 8.7154294504188129325e-66,
  1.7916237278037667488e-17, 2.5454160968749269937e-33, 2.1132476107887107169e-49, 8.7154294504188129325e-66,
  4.0384494702232122736e-18, 1.0046721413651383112e-33, 2.1132476107887107169e-49, 8.7154294504188129325e-66,
  4.0384494702232122736e-18, 1.0046721413651383112e-33, 2.1132476107887107169e-49, 8.7154294504188129325e-66,
  5.6900251826959904774e-19, 4.1707169171520598517e-35, -2.4964157285045710972e-51, -1.866653112309982615e-67,
  5.6900251826959904774e-19, 4.1707169171520598517e-35, -2.4964157285045710972e-51, -1.866653112309982615e-67,
  5.6900251826959904774e-19, 4.1707169171520598517e-35, -2.4964157285045710972e-51, -1.866653112309982615e-67,
  1.3532164927539732229e-19, -6.4410794381603004826e-36, 1.7634898158762432635e-52, 3.5887057810247033998e-68,
  1.3532164927539732229e-19, -6.4410794381603004826e-36, 1.7634898158762432635e-52, 3.5887057810247033998e-68,
  2.6901432026846872871e-20, -4.2254836195018827479e-37, 9.3011872068621332399e-54, 1.113250147552460308e-69,
  2.6901432026846872871e-20, -4.2254836195018827479e-37, 9.3011872068621332399e-54, 1.113250147552460308e-69,
  2.6901432026846872871e-20, -4.2254836195018827479e-37, 9.3011872068621332399e-54, 1.113250147552460308e-69,
  1.3348904870778067446e-20, -4.2254836195018827479e-37, 9.3011872068621332399e-54, 1.113250147552460308e-69,
  6.5726412927436632287e-21, 1.0820844071023395684e-36, 1.7634898158762432635e-52, 3.5887057810247033998e-68,
  3.1845095037264626247e-21, 3.2976802257607573031e-37, 9.3011872068621332399e-54, 1.113250147552460308e-69,
  1.4904436092178623228e-21, -4.6390169687056261795e-38, -1.1392999419355048437e-54, -4.587677453735884283e-71,
  6.4341066196356198368e-22, -4.6390169687056261795e-38, -1.1392999419355048437e-54, -4.587677453735884283e-71,
  2.1989418833641172011e-22, 4.7649378378726728402e-38, 9.3011872068621332399e-54, 1.113250147552460308e-69,
  8.135951522836682362e-24, 6.2960434583523738135e-40, 2.6283399642369020339e-57, 5.3358074162805516304e-73,
  8.135951522836682362e-24, 6.2960434583523738135e-40, 2.6283399642369020339e-57, 5.3358074162805516304e-73,
  8.135951522836682362e-24, 6.2960434583523738135e-40, 2.6283399642369020339e-57, 5.3358074162805516304e-73,
  8.135951522836682362e-24, 6.2960434583523738135e-40, 2.6283399642369020339e-57, 5.3358074162805516304e-73,
  8.135951522836682362e-24, 6.2960434583523738135e-40, 2.6283399642369020339e-57, 5.3358074162805516304e-73,
  1.5185066224124613304e-24, 2.6226236120327253511e-40, 2.6283399642369020339e-57, 5.3358074162805516304e-73,
  1.5185066224124613304e-24, 2.6226236120327253511e-40, 2.6283399642369020339e-57, 5.3358074162805516304e-73,
  1.5185066224124613304e-24, 2.6226236120327253511e-40, 2.6283399642369020339e-57, 5.3358074162805516304e-73,
  6.9132600985943383921e-25, 7.8591368887290111994e-41, 2.6283399642369020339e-57, 5.3358074162805516304e-73,
  2.7773570358292009361e-25, -1.3244127270701094468e-41, -2.4695541513869446866e-57, -3.2399200798614356002e-74,
  7.0940550444663151936e-26, 9.7147467687967058732e-42, 7.9392906424978921242e-59, 2.9745456030524896742e-75,
  7.0940550444663151936e-26, 9.7147467687967058732e-42, 7.9392906424978921242e-59, 2.9745456030524896742e-75,
  1.9241762160098927996e-26, 3.9750282589222551507e-42, 7.9392906424978921242e-59, 2.9745456030524896742e-75,
  1.9241762160098927996e-26, 3.9750282589222551507e-42, 7.9392906424978921242e-59, 2.9745456030524896742e-75,
  6.317065088957874881e-27, -3.2976062348358281152e-43, -2.6168913164368963837e-61, 3.7036201000008290615e-78,
  6.317065088957874881e-27, -3.2976062348358281152e-43, -2.6168913164368963837e-61, 3.7036201000008290615e-78,
  3.0858908211726098086e-27, 3.8770419025072344914e-43, 7.9392906424978921242e-59, 2.9745456030524896742e-75,
  1.4703036872799779898e-27, 2.8971783383570358633e-44, -2.6168913164368963837e-61, 3.7036201000008290615e-78,
  6.625101203336619011e-28, 2.8971783383570358633e-44, -2.6168913164368963837e-61, 3.7036201000008290615e-78,
  2.5861333686050385673e-28, 2.8971783383570358633e-44, -2.6168913164368963837e-61, 3.7036201000008290615e-78,
  5.6664945123924856962e-29, 6.5510079543732854985e-45, -2.6168913164368963837e-61, 3.7036201000008290615e-78,
  5.6664945123924856962e-29, 6.5510079543732854985e-45, -2.6168913164368963837e-61, 3.7036201000008290615e-78,
  6.1778471897801070206e-30, 9.4581409707401690366e-46, 4.9461632249367446986e-62, 3.7036201000008290615e-78,
  6.1778471897801070206e-30, 9.4581409707401690366e-46, 4.9461632249367446986e-62, 3.7036201000008290615e-78,
  6.1778471897801070206e-30, 9.4581409707401690366e-46, 4.9461632249367446986e-62, 3.7036201000008290615e-78,
  6.1778471897801070206e-30, 9.4581409707401690366e-46, 4.9461632249367446986e-62, 3.7036201000008290615e-78,
  3.0224035688960604996e-30, 2.451648649116083682e-46, 4.9461632249367446986e-62, 3.7036201000008290615e-78,
  1.4446817584540368888e-30, 2.451648649116083682e-46, 4.9461632249367446986e-62, 3.7036201000008290615e-78,
  6.5582085323302525856e-31, 7.0002556871006273225e-47, 1.0567786762735315635e-62, -6.1446417754639313137e-79,
  2.6139040062251944343e-31, -1.7578597149294783985e-47, 8.4432539107728090768e-64, 1.9517662449371102229e-79,
  6.4175174317266470186e-32, 4.3166913557804827486e-48, 8.4432539107728090768e-64, 1.9517662449371102229e-79,
  6.4175174317266470186e-32, 4.3166913557804827486e-48, 8.4432539107728090768e-64, 1.9517662449371102229e-79,
  1.4871367740953237822e-32, -1.1571307704883330232e-48, -6.7249112515659569668e-65, -7.2335760163150273591e-81,
  1.4871367740953237822e-32, -1.1571307704883330232e-48, -6.7249112515659569668e-65, -7.2335760163150273591e-81,
  2.5454160968749269937e-33, 2.1132476107887107169e-49, 8.7154294504188118783e-66, 1.2001823382693912203e-81,
  2.5454160968749269937e-33, 2.1132476107887107169e-49, 8.7154294504188118783e-66, 1.2001823382693912203e-81,
  2.5454160968749269937e-33, 2.1132476107887107169e-49, 8.7154294504188118783e-66, 1.2001823382693912203e-81,
  1.0046721413651383112e-33, 2.1132476107887107169e-49, 8.7154294504188118783e-66, 1.2001823382693912203e-81,
  2.3430016361024414106e-34, 4.0267819632970559834e-50, -7.8013829534098555144e-67, -1.1759240463442418271e-82,
  2.3430016361024414106e-34, 4.0267819632970559834e-50, -7.8013829534098555144e-67, -1.1759240463442418271e-82,
  4.1707169171520598517e-35, -2.4964157285045710972e-51, -1.866653112309982615e-67, 1.4185069655957361252e-83,
  4.1707169171520598517e-35, -2.4964157285045710972e-51, -1.866653112309982615e-67, 1.4185069655957361252e-83,
  4.1707169171520598517e-35, -2.4964157285045710972e-51, -1.866653112309982615e-67, 1.4185069655957361252e-83,
  1.7633044866680145008e-35, 2.8491136916798196016e-51, 4.0680767287898916022e-67, 1.4185069655957361252e-83,
  5.595982714259923599e-36, 1.7634898158762432635e-52, 3.588705781024702988e-68, 5.9489775128085140685e-84,
  5.595982714259923599e-36, 1.7634898158762432635e-52, 3.588705781024702988e-68, 5.9489775128085140685e-84,
  2.5867171761548675786e-36, 1.7634898158762432635e-52, 3.588705781024702988e-68, 5.9489775128085140685e-84,
  1.0820844071023395684e-36, 1.7634898158762432635e-52, 3.588705781024702988e-68, 5.9489775128085140685e-84,
  3.2976802257607573031e-37, 9.3011872068621332399e-54, 1.113250147552460308e-69, 2.9286284920280944778e-86,
  3.2976802257607573031e-37, 9.3011872068621332399e-54, 1.113250147552460308e-69, 2.9286284920280944778e-86,
  1.4168892644450972904e-37, 9.3011872068621332399e-54, 1.113250147552460308e-69, 2.9286284920280944778e-86,
  4.7649378378726728402e-38, 9.3011872068621332399e-54, 1.113250147552460308e-69, 2.9286284920280944778e-86,
  6.2960434583523738135e-40, 2.6283399642369020339e-57, 5.3358074162805516304e-73, 4.524218473063975309e-90,
  6.2960434583523738135e-40, 2.6283399642369020339e-57, 5.3358074162805516304e-73, 4.524218473063975309e-90,
  6.2960434583523738135e-40, 2.6283399642369020339e-57, 5.3358074162805516304e-73, 4.524218473063975309e-90,
  6.2960434583523738135e-40, 2.6283399642369020339e-57, 5.3358074162805516304e-73, 4.524218473063975309e-90,
  6.2960434583523738135e-40, 2.6283399642369020339e-57, 5.3358074162805516304e-73, 4.524218473063975309e-90,
  6.2960434583523738135e-40, 2.6283399642369020339e-57, 5.3358074162805516304e-73, 4.524218473063975309e-90,
  6.2960434583523738135e-40, 2.6283399642369020339e-57, 5.3358074162805516304e-73, 4.524218473063975309e-90,
  2.6226236120327253511e-40, 2.6283399642369020339e-57, 5.3358074162805516304e-73, 4.524218473063975309e-90,
  7.8591368887290111994e-41, 2.6283399642369020339e-57, 5.3358074162805516304e-73, 4.524218473063975309e-90,
  7.8591368887290111994e-41, 2.6283399642369020339e-57, 5.3358074162805516304e-73, 4.524218473063975309e-90,
  3.2673620808294506214e-41, 2.6283399642369020339e-57, 5.3358074162805516304e-73, 4.524218473063975309e-90,
  9.7147467687967058732e-42, 7.9392906424978921242e-59, 2.9745456030524891833e-75, 5.969437008257943935e-91,
  9.7147467687967058732e-42, 7.9392906424978921242e-59, 2.9745456030524891833e-75, 5.969437008257943935e-91,
  3.9750282589222551507e-42, 7.9392906424978921242e-59, 2.9745456030524891833e-75, 5.969437008257943935e-91,
  1.1051690039850297894e-42, 7.9392906424978921242e-59, 2.9745456030524891833e-75, 5.969437008257943935e-91,
  1.1051690039850297894e-42, 7.9392906424978921242e-59, 2.9745456030524891833e-75, 5.969437008257943935e-91,
  3.8770419025072344914e-43, 7.9392906424978921242e-59, 2.9745456030524891833e-75, 5.969437008257943935e-91,
  2.8971783383570358633e-44, -2.6168913164368963837e-61, 3.7036201000008285821e-78, 5.6554937751584084315e-94,
  2.8971783383570358633e-44, -2.6168913164368963837e-61, 3.7036201000008285821e-78, 5.6554937751584084315e-94,
  2.8971783383570358633e-44, -2.6168913164368963837e-61, 3.7036201000008285821e-78, 5.6554937751584084315e-94,
  2.8971783383570358633e-44, -2.6168913164368963837e-61, 3.7036201000008285821e-78, 5.6554937751584084315e-94,
  6.5510079543732854985e-45, -2.6168913164368963837e-61, 3.7036201000008285821e-78, 5.6554937751584084315e-94,
  6.5510079543732854985e-45, -2.6168913164368963837e-61, 3.7036201000008285821e-78, 5.6554937751584084315e-94,
  9.4581409707401690366e-46, 4.9461632249367446986e-62, 3.7036201000008285821e-78, 5.6554937751584084315e-94,
  9.4581409707401690366e-46, 4.9461632249367446986e-62, 3.7036201000008285821e-78, 5.6554937751584084315e-94,
  9.4581409707401690366e-46, 4.9461632249367446986e-62, 3.7036201000008285821e-78, 5.6554937751584084315e-94,
  2.451648649116083682e-46, 4.9461632249367446986e-62, 3.7036201000008285821e-78, 5.6554937751584084315e-94,
  2.451648649116083682e-46, 4.9461632249367446986e-62, 3.7036201000008285821e-78, 5.6554937751584084315e-94,
  7.0002556871006273225e-47, 1.0567786762735315635e-62, -6.1446417754639301152e-79, -1.5355611056488084652e-94,
  7.0002556871006273225e-47, 1.0567786762735315635e-62, -6.1446417754639301152e-79, -1.5355611056488084652e-94,
  2.6211979860855749482e-47, 8.4432539107728090768e-64, 1.9517662449371099233e-79, 2.62202614552995759e-95,
  4.3166913557804827486e-48, 8.4432539107728090768e-64, 1.9517662449371099233e-79, 2.62202614552995759e-95,
  4.3166913557804827486e-48, 8.4432539107728090768e-64, 1.9517662449371099233e-79, 2.62202614552995759e-95,
  4.3166913557804827486e-48, 8.4432539107728090768e-64, 1.9517662449371099233e-79, 2.62202614552995759e-95,
  1.5797802926460750146e-48, 2.3660905534865399025e-64, -7.2335760163150273591e-81, 2.8738690232659205689e-99,
  2.1132476107887107169e-49, 8.7154294504188118783e-66, 1.2001823382693912203e-81, 2.8738690232659205689e-99,
  2.1132476107887107169e-49, 8.7154294504188118783e-66, 1.2001823382693912203e-81, 2.8738690232659205689e-99,
  2.1132476107887107169e-49, 8.7154294504188118783e-66, 1.2001823382693912203e-81, 2.8738690232659205689e-99,
  4.0267819632970559834e-50, -7.8013829534098555144e-67, -1.1759240463442418271e-82, 2.8738690232659205689e-99,
  4.0267819632970559834e-50, -7.8013829534098555144e-67, -1.1759240463442418271e-82, 2.8738690232659205689e-99,
  4.0267819632970559834e-50, -7.8013829534098555144e-67, -1.1759240463442418271e-82, 2.8738690232659205689e-99,
  1.8885701952232994665e-50, -7.8013829534098555144e-67, -1.1759240463442418271e-82, 2.8738690232659205689e-99,
  8.1946431118642097069e-51, 1.5937536410989638719e-66, 1.459625439463388979e-82, 2.8738690232659205689e-99,
  2.8491136916798196016e-51, 4.0680767287898916022e-67, 1.4185069655957361252e-83, -7.8369062883735917115e-100,
  1.7634898158762432635e-52, 3.588705781024702988e-68, 5.9489775128085131541e-84, 1.0450891972142808004e-99,
  1.7634898158762432635e-52, 3.588705781024702988e-68, 5.9489775128085131541e-84, 1.0450891972142808004e-99,
  1.7634898158762432635e-52, 3.588705781024702988e-68, 5.9489775128085131541e-84, 1.0450891972142808004e-99,
  1.7634898158762432635e-52, 3.588705781024702988e-68, 5.9489775128085131541e-84, 1.0450891972142808004e-99,
  9.3011872068621332399e-54, 1.113250147552460308e-69, 2.9286284920280941206e-86, 2.1132026692048600853e-102,
  9.3011872068621332399e-54, 1.113250147552460308e-69, 2.9286284920280941206e-86, 2.1132026692048600853e-102,
  9.3011872068621332399e-54, 1.113250147552460308e-69, 2.9286284920280941206e-86, 2.1132026692048600853e-102,
  9.3011872068621332399e-54, 1.113250147552460308e-69, 2.9286284920280941206e-86, 2.1132026692048600853e-102,
  9.3011872068621332399e-54, 1.113250147552460308e-69, 2.9286284920280941206e-86, 2.1132026692048600853e-102,
  4.0809436324633147776e-54, -4.587677453735884283e-71, -2.8859500138942368532e-87, -5.6567402911297190423e-103,
  1.470821845263904967e-54, -4.587677453735884283e-71, -2.8859500138942368532e-87, -5.6567402911297190423e-103,
  1.6576095166419998917e-55, 2.6568658093254848067e-71, 5.1571087196495574384e-87, 3.2728487032630537605e-103,
  1.6576095166419998917e-55, 2.6568658093254848067e-71, 5.1571087196495574384e-87, 3.2728487032630537605e-103,
  1.6576095166419998917e-55, 2.6568658093254848067e-71, 5.1571087196495574384e-87, 3.2728487032630537605e-103,
  2.6283399642369020339e-57, 5.3358074162805516304e-73, 4.5242184730639744369e-90, 1.145584788913072936e-105,
  2.6283399642369020339e-57, 5.3358074162805516304e-73, 4.5242184730639744369e-90, 1.145584788913072936e-105,
  2.6283399642369020339e-57, 5.3358074162805516304e-73, 4.5242184730639744369e-90, 1.145584788913072936e-105,
  2.6283399642369020339e-57, 5.3358074162805516304e-73, 4.5242184730639744369e-90, 1.145584788913072936e-105,
  2.6283399642369020339e-57, 5.3358074162805516304e-73, 4.5242184730639744369e-90, 1.145584788913072936e-105,
  2.6283399642369020339e-57, 5.3358074162805516304e-73, 4.5242184730639744369e-90, 1.145584788913072936e-105,
  7.9392906424978921242e-59, 2.9745456030524891833e-75, 5.969437008257942845e-91, 5.554706987098633963e-107,
  7.9392906424978921242e-59, 2.9745456030524891833e-75, 5.969437008257942845e-91, 5.554706987098633963e-107,
  7.9392906424978921242e-59, 2.9745456030524891833e-75, 5.969437008257942845e-91, 5.554706987098633963e-107,
  7.9392906424978921242e-59, 2.9745456030524891833e-75, 5.969437008257942845e-91, 5.554706987098633963e-107,
  7.9392906424978921242e-59, 2.9745456030524891833e-75, 5.969437008257942845e-91, 5.554706987098633963e-107,
  7.9392906424978921242e-59, 2.9745456030524891833e-75, 5.969437008257942845e-91, 5.554706987098633963e-107,
  3.9565608646667614317e-59, 2.9745456030524891833e-75, 5.969437008257942845e-91, 5.554706987098633963e-107,
  1.9651959757511960854e-59, 2.9745456030524891833e-75, 5.969437008257942845e-91, 5.554706987098633963e-107,
  9.6951353129341363331e-60, 7.6368645294831185015e-76, 1.0603435429602168369e-91, 1.0451839188820145747e-108,
  4.7167230906452229674e-60, 7.6368645294831185015e-76, 1.0603435429602168369e-91, 1.0451839188820145747e-108,
  2.2275169795007668372e-60, 2.1097166542226745549e-76, 4.4670685979800101779e-92, 1.0451839188820145747e-108,
  9.8291392392853877215e-61, -6.5385728340754726503e-77, -1.3520652573660833788e-93, -2.3220403312043059402e-109,
  3.6061239614242446325e-61, 7.2792968540756372162e-77, 1.3988851821689310822e-92, 1.0451839188820145747e-108,
  4.9461632249367446986e-62, 3.7036201000008285821e-78, 5.6554937751584084315e-94, -1.9306041120023063932e-110,
  4.9461632249367446986e-62, 3.7036201000008285821e-78, 5.6554937751584084315e-94, -1.9306041120023063932e-110,
  4.9461632249367446986e-62, 3.7036201000008285821e-78, 5.6554937751584084315e-94, -1.9306041120023063932e-110,
  1.0567786762735315635e-62, -6.1446417754639301152e-79, -1.535561105648808199e-94, -1.9306041120023063932e-110,
  1.0567786762735315635e-62, -6.1446417754639301152e-79, -1.535561105648808199e-94, -1.9306041120023063932e-110,
  8.4432539107728090768e-64, 1.9517662449371099233e-79, 2.62202614552995759e-95, 6.5314563001514358328e-112,
  8.4432539107728090768e-64, 1.9517662449371099233e-79, 2.62202614552995759e-95, 6.5314563001514358328e-112,
  8.4432539107728090768e-64, 1.9517662449371099233e-79, 2.62202614552995759e-95, 6.5314563001514358328e-112,
  8.4432539107728090768e-64, 1.9517662449371099233e-79, 2.62202614552995759e-95, 6.5314563001514358328e-112,
  2.3660905534865399025e-64, -7.2335760163150273591e-81, 2.8738690232659205689e-99, 1.8395411057335783574e-115,
  2.3660905534865399025e-64, -7.2335760163150273591e-81, 2.8738690232659205689e-99, 1.8395411057335783574e-115,
  8.4679971416497210292e-65, -7.2335760163150273591e-81, 2.8738690232659205689e-99, 1.8395411057335783574e-115,
  8.7154294504188118783e-66, 1.2001823382693912203e-81, 2.8738690232659205689e-99, 1.8395411057335783574e-115,
  8.7154294504188118783e-66, 1.2001823382693912203e-81, 2.8738690232659205689e-99, 1.8395411057335783574e-115,
  8.7154294504188118783e-66, 1.2001823382693912203e-81, 2.8738690232659205689e-99, 1.8395411057335783574e-115,
  8.7154294504188118783e-66, 1.2001823382693912203e-81, 2.8738690232659205689e-99, 1.8395411057335783574e-115,
  3.9676455775389135587e-66, 1.459625439463388979e-82, 2.8738690232659205689e-99, 1.8395411057335783574e-115,
  1.5937536410989638719e-66, 1.459625439463388979e-82, 2.8738690232659205689e-99, 1.8395411057335783574e-115,
  4.0680767287898916022e-67, 1.4185069655957361252e-83, -7.8369062883735917115e-100, -1.9081236411894110579e-116,
  4.0680767287898916022e-67, 1.4185069655957361252e-83, -7.8369062883735917115e-100, -1.9081236411894110579e-116,
  1.1007118082399544936e-67, 1.4185069655957361252e-83, -7.8369062883735917115e-100, -1.9081236411894110579e-116,
  1.1007118082399544936e-67, 1.4185069655957361252e-83, -7.8369062883735917115e-100, -1.9081236411894110579e-116,
  3.588705781024702988e-68, 5.9489775128085131541e-84, 1.0450891972142805974e-99, 1.8395411057335783574e-115,
  3.588705781024702988e-68, 5.9489775128085131541e-84, 1.0450891972142805974e-99, 1.8395411057335783574e-115,
  1.7341027056809927069e-68, 1.830931441234090934e-84, 1.3069928418846076386e-100, 3.1677600334418876704e-116,
  8.0680116800913756637e-69, -2.2809159455312046184e-85, -4.0748824503880445403e-101, -6.3915272253158644628e-117,
  3.4315039917320989315e-69, -2.2809159455312046184e-85, -4.0748824503880445403e-101, -6.3915272253158644628e-117,
  1.113250147552460308e-69, 2.9286284920280941206e-86, 2.1132026692048600853e-102, -4.6672632026740766185e-119,
  1.113250147552460308e-69, 2.9286284920280941206e-86, 2.1132026692048600853e-102, -4.6672632026740766185e-119,
  5.3368668650755071652e-70, 2.9286284920280941206e-86, 2.1132026692048600853e-102, -4.6672632026740766185e-119,
  2.4390495598509592076e-70, 2.9286284920280941206e-86, 2.1132026692048600853e-102, -4.6672632026740766185e-119,
  9.901409072386855505e-71, -2.8859500138942368532e-87, -5.6567402911297190423e-103, -4.6672632026740766185e-119,
  2.6568658093254848067e-71, 5.1571087196495574384e-87, 3.2728487032630532648e-103, 5.2465720993401781599e-119,
  2.6568658093254848067e-71, 5.1571087196495574384e-87, 3.2728487032630532648e-103, 5.2465720993401781599e-119,
  8.4572999356014273536e-72, 1.1355793528776598461e-87, 3.2728487032630532648e-103, 5.2465720993401781599e-119,
  8.4572999356014273536e-72, 1.1355793528776598461e-87, 3.2728487032630532648e-103, 5.2465720993401781599e-119,
  3.9294603961880721752e-72, 1.3019701118468578292e-88, -7.5747169634236195447e-105, -2.0152904854894729832e-121,
  1.6655406264813940833e-72, 1.3019701118468578292e-88, -7.5747169634236195447e-105, -2.0152904854894729832e-121,
  5.3358074162805516304e-73, 4.5242184730639744369e-90, 1.1455847889130727424e-105, 1.8573014293598455046e-121,
  5.3358074162805516304e-73, 4.5242184730639744369e-90, 1.1455847889130727424e-105, 1.8573014293598455046e-121,
  2.5059077041472040156e-73, 4.5242184730639744369e-90, 1.1455847889130727424e-105, 1.8573014293598455046e-121,
  1.0909578480805302081e-73, 4.5242184730639744369e-90, 1.1455847889130727424e-105, 1.8573014293598455046e-121,
  3.8348292004719330442e-74, 4.5242184730639744369e-90, 1.1455847889130727424e-105, 1.8573014293598455046e-121,
  2.9745456030524891833e-75, 5.969437008257942845e-91, 5.5547069870986327528e-107, 1.6304246661326865276e-122,
  2.9745456030524891833e-75, 5.969437008257942845e-91, 5.5547069870986327528e-107, 1.6304246661326865276e-122,
  2.9745456030524891833e-75, 5.969437008257942845e-91, 5.5547069870986327528e-107, 1.6304246661326865276e-122,
  2.9745456030524891833e-75, 5.969437008257942845e-91, 5.5547069870986327528e-107, 1.6304246661326865276e-122,
  7.6368645294831185015e-76, 1.0603435429602168369e-91, 1.0451839188820145747e-108, 4.2386081393205242443e-125,
  7.6368645294831185015e-76, 1.0603435429602168369e-91, 1.0451839188820145747e-108, 4.2386081393205242443e-125,
  2.1097166542226745549e-76, 4.4670685979800101779e-92, 1.0451839188820145747e-108, 4.2386081393205242443e-125,
  2.1097166542226745549e-76, 4.4670685979800101779e-92, 1.0451839188820145747e-108, 4.2386081393205242443e-125,
  7.2792968540756372162e-77, 1.3988851821689310822e-92, 1.0451839188820145747e-108, 4.2386081393205242443e-125,
  3.7036201000008285821e-78, 5.6554937751584084315e-94, -1.9306041120023063932e-110, 1.0223371855251472933e-126,
  3.7036201000008285821e-78, 5.6554937751584084315e-94, -1.9306041120023063932e-110, 1.0223371855251472933e-126,
  3.7036201000008285821e-78, 5.6554937751584084315e-94, -1.9306041120023063932e-110, 1.0223371855251472933e-126,
  3.7036201000008285821e-78, 5.6554937751584084315e-94, -1.9306041120023063932e-110, 1.0223371855251472933e-126,
  3.7036201000008285821e-78, 5.6554937751584084315e-94, -1.9306041120023063932e-110, 1.0223371855251472933e-126,
  1.5445779612272179051e-78, 8.6145718795359707834e-95, 7.3062078800278780675e-111, 1.0223371855251472933e-126,
  4.6505689184041232695e-79, 8.6145718795359707834e-95, 7.3062078800278780675e-111, 1.0223371855251472933e-126,
  4.6505689184041232695e-79, 8.6145718795359707834e-95, 7.3062078800278780675e-111, 1.0223371855251472933e-126,
  1.9517662449371099233e-79, 2.62202614552995759e-95, 6.5314563001514349095e-112, 9.9039323746573674262e-128,
  6.0236490820360325022e-80, -3.7424672147304925625e-96, -1.784871512364483542e-112, 6.7095375687163151728e-129,
  6.0236490820360325022e-80, -3.7424672147304925625e-96, -1.784871512364483542e-112, 6.7095375687163151728e-129,
  2.6501457402022643213e-80, 3.7482149527770239293e-96, 6.5314563001514349095e-112, 9.9039323746573674262e-128,
  9.6339406928538097998e-81, 2.8738690232659205689e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132,
  1.2001823382693912203e-81, 2.8738690232659205689e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132,
  1.2001823382693912203e-81, 2.8738690232659205689e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132,
  1.2001823382693912203e-81, 2.8738690232659205689e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132,
  1.459625439463388979e-82, 2.8738690232659205689e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132,
  1.459625439463388979e-82, 2.8738690232659205689e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132,
  1.459625439463388979e-82, 2.8738690232659205689e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132,
  1.4185069655957361252e-83, -7.8369062883735917115e-100, -1.9081236411894107761e-116, -2.1796760241698337334e-132,
  1.4185069655957361252e-83, -7.8369062883735917115e-100, -1.9081236411894107761e-116, -2.1796760241698337334e-132,
  1.4185069655957361252e-83, -7.8369062883735917115e-100, -1.9081236411894107761e-116, -2.1796760241698337334e-132,
  1.4185069655957361252e-83, -7.8369062883735917115e-100, -1.9081236411894107761e-116, -2.1796760241698337334e-132,
  5.9489775128085131541e-84, 1.0450891972142805974e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132,
  1.830931441234090934e-84, 1.3069928418846076386e-100, 3.1677600334418871069e-116, 3.4556869017247800778e-132,
  1.830931441234090934e-84, 1.3069928418846076386e-100, 3.1677600334418871069e-116, 3.4556869017247800778e-132,
  8.0141992334048515034e-85, 1.3069928418846076386e-100, 3.1677600334418871069e-116, 3.4556869017247800778e-132,
  2.8666416439368237283e-85, 1.6400545060233297363e-101, -4.6672632026740766185e-119, -3.755176715260116501e-136,
  2.9286284920280941206e-86, 2.1132026692048600853e-102, -4.6672632026740766185e-119, -3.755176715260116501e-136,
  2.9286284920280941206e-86, 2.1132026692048600853e-102, -4.6672632026740766185e-119, -3.755176715260116501e-136,
  2.9286284920280941206e-86, 2.1132026692048600853e-102, -4.6672632026740766185e-119, -3.755176715260116501e-136,
  2.9286284920280941206e-86, 2.1132026692048600853e-102, -4.6672632026740766185e-119, -3.755176715260116501e-136,
  1.3200167453193350837e-86, 2.1132026692048600853e-102, -4.6672632026740766185e-119, -3.755176715260116501e-136,
  5.1571087196495574384e-87, 3.2728487032630532648e-103, 5.2465720993401781599e-119, -3.755176715260116501e-136,
  1.1355793528776598461e-87, 3.2728487032630532648e-103, 5.2465720993401781599e-119, -3.755176715260116501e-136,
  1.1355793528776598461e-87, 3.2728487032630532648e-103, 5.2465720993401781599e-119, -3.755176715260116501e-136,
  1.3019701118468578292e-88, -7.5747169634236195447e-105, -2.0152904854894725532e-121, -3.1562414818576682143e-137,
  1.3019701118468578292e-88, -7.5747169634236195447e-105, -2.0152904854894725532e-121, -3.1562414818576682143e-137,
  1.3019701118468578292e-88, -7.5747169634236195447e-105, -2.0152904854894725532e-121, -3.1562414818576682143e-137,
  4.5242184730639744369e-90, 1.1455847889130727424e-105, 1.8573014293598452896e-121, 1.1431992269852683481e-137,
  4.5242184730639744369e-90, 1.1455847889130727424e-105, 1.8573014293598452896e-121, 1.1431992269852683481e-137,
  4.5242184730639744369e-90, 1.1455847889130727424e-105, 1.8573014293598452896e-121, 1.1431992269852683481e-137,
  4.5242184730639744369e-90, 1.1455847889130727424e-105, 1.8573014293598452896e-121, 1.1431992269852683481e-137,
  4.5242184730639744369e-90, 1.1455847889130727424e-105, 1.8573014293598452896e-121, 1.1431992269852683481e-137,
  5.969437008257942845e-91, 5.5547069870986327528e-107, 1.6304246661326865276e-122, 6.8339049774534162772e-139,
  5.969437008257942845e-91, 5.5547069870986327528e-107, 1.6304246661326865276e-122, 6.8339049774534162772e-139,
  5.969437008257942845e-91, 5.5547069870986327528e-107, 1.6304246661326865276e-122, 6.8339049774534162772e-139,
  1.0603435429602168369e-91, 1.0451839188820145747e-108, 4.2386081393205242443e-125, 1.1062055705591188256e-141,
  1.0603435429602168369e-91, 1.0451839188820145747e-108, 4.2386081393205242443e-125, 1.1062055705591188256e-141,
  1.0603435429602168369e-91, 1.0451839188820145747e-108, 4.2386081393205242443e-125, 1.1062055705591188256e-141,
  4.4670685979800101779e-92, 1.0451839188820145747e-108, 4.2386081393205242443e-125, 1.1062055705591188256e-141,
  1.3988851821689310822e-92, 1.0451839188820145747e-108, 4.2386081393205242443e-125, 1.1062055705591188256e-141,
  1.3988851821689310822e-92, 1.0451839188820145747e-108, 4.2386081393205242443e-125, 1.1062055705591188256e-141,
  6.3183932821616130831e-93, 1.0451839188820145747e-108, 4.2386081393205242443e-125, 1.1062055705591188256e-141,
  2.4831640123977650651e-93, 1.9359195088038447797e-109, -4.8867691298577234423e-126, -2.0587960670007823264e-142,
  5.6554937751584084315e-94, -1.9306041120023063932e-110, 1.0223371855251471293e-126, 1.2214168761472102282e-142,
  5.6554937751584084315e-94, -1.9306041120023063932e-110, 1.0223371855251471293e-126, 1.2214168761472102282e-142,
  8.6145718795359707834e-95, 7.3062078800278780675e-111, 1.0223371855251471293e-126, 1.2214168761472102282e-142,
  8.6145718795359707834e-95, 7.3062078800278780675e-111, 1.0223371855251471293e-126, 1.2214168761472102282e-142,
  8.6145718795359707834e-95, 7.3062078800278780675e-111, 1.0223371855251471293e-126, 1.2214168761472102282e-142,
  2.62202614552995759e-95, 6.5314563001514349095e-112, 9.9039323746573674262e-128, -8.6629775332868987041e-145,
  2.62202614552995759e-95, 6.5314563001514349095e-112, 9.9039323746573674262e-128, -8.6629775332868987041e-145,
  1.1238897120284541253e-95, 6.5314563001514349095e-112, 9.9039323746573674262e-128, -8.6629775332868987041e-145,
  3.7482149527770239293e-96, 6.5314563001514349095e-112, 9.9039323746573674262e-128, -8.6629775332868987041e-145,
  2.8738690232659205689e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132, -3.9681466199873824165e-148,
  2.8738690232659205689e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132, -3.9681466199873824165e-148,
  2.8738690232659205689e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132, -3.9681466199873824165e-148,
  2.8738690232659205689e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132, -3.9681466199873824165e-148,
  2.8738690232659205689e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132, -3.9681466199873824165e-148,
  2.8738690232659205689e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132, -3.9681466199873824165e-148,
  2.8738690232659205689e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132, -3.9681466199873824165e-148,
  2.8738690232659205689e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132, -3.9681466199873824165e-148,
  2.8738690232659205689e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132, -3.9681466199873824165e-148,
  2.8738690232659205689e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132, -3.9681466199873824165e-148,
  2.8738690232659205689e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132, -3.9681466199873824165e-148,
  1.0450891972142805974e-99, 1.8395411057335783574e-115, -7.8150389500644475446e-132, -3.9681466199873824165e-148,
  1.3069928418846076386e-100, 3.1677600334418871069e-116, 3.4556869017247794521e-132, 8.5448727249069983612e-148,
  1.3069928418846076386e-100, 3.1677600334418871069e-116, 3.4556869017247794521e-132, 8.5448727249069983612e-148,
  1.3069928418846076386e-100, 3.1677600334418871069e-116, 3.4556869017247794521e-132, 8.5448727249069983612e-148,
  1.6400545060233297363e-101, -4.6672632026740766185e-119, -3.755176715260116501e-136, 2.1571619860435652883e-152,
  1.6400545060233297363e-101, -4.6672632026740766185e-119, -3.755176715260116501e-136, 2.1571619860435652883e-152,
  1.6400545060233297363e-101, -4.6672632026740766185e-119, -3.755176715260116501e-136, 2.1571619860435652883e-152,
  2.1132026692048600853e-102, -4.6672632026740766185e-119, -3.755176715260116501e-136, 2.1571619860435652883e-152,
  2.1132026692048600853e-102, -4.6672632026740766185e-119, -3.755176715260116501e-136, 2.1571619860435652883e-152,
  2.1132026692048600853e-102, -4.6672632026740766185e-119, -3.755176715260116501e-136, 2.1571619860435652883e-152,
  3.2728487032630532648e-103, 5.2465720993401781599e-119, -3.755176715260116501e-136, 2.1571619860435652883e-152,
  3.2728487032630532648e-103, 5.2465720993401781599e-119, -3.755176715260116501e-136, 2.1571619860435652883e-152,
  3.2728487032630532648e-103, 5.2465720993401781599e-119, -3.755176715260116501e-136, 2.1571619860435652883e-152,
  1.0404514546648604359e-103, 2.896544483330507019e-120, 3.1239284188885823808e-136, 2.1571619860435652883e-152,
  1.0404514546648604359e-103, 2.896544483330507019e-120, 3.1239284188885823808e-136, 2.1571619860435652883e-152,
  4.8235214251531210473e-104, 2.896544483330507019e-120, 3.1239284188885823808e-136, 2.1571619860435652883e-152,
  2.0330248644053793915e-104, 2.896544483330507019e-120, 3.1239284188885823808e-136, 2.1571619860435652883e-152,
  6.3777658403150887343e-105, -2.0152904854894725532e-121, -3.156241481857667737e-137, -7.0684085473731388916e-153,
  6.3777658403150887343e-105, -2.0152904854894725532e-121, -3.156241481857667737e-137, -7.0684085473731388916e-153,
  2.88964513938041089e-105, 5.7298933442091639924e-121, -3.156241481857667737e-137, -7.0684085473731388916e-153,
  1.1455847889130727424e-105, 1.8573014293598452896e-121, 1.1431992269852681095e-137, 2.4782675885631257398e-153,
  2.7355461367940366859e-106, -7.8994528064813712419e-123, -2.0037599452814940222e-138, 9.1598554579059548847e-155,
  2.7355461367940366859e-106, -7.8994528064813712419e-123, -2.0037599452814940222e-138, 9.1598554579059548847e-155,
  5.5547069870986327528e-107, 1.6304246661326865276e-122, 6.8339049774534147855e-139, 9.1598554579059548847e-155,
  5.5547069870986327528e-107, 1.6304246661326865276e-122, 6.8339049774534147855e-139, 9.1598554579059548847e-155,
  1.0451839188820145747e-108, 4.2386081393205242443e-125, 1.1062055705591186799e-141, 1.1734404793201255869e-157,
  1.0451839188820145747e-108, 4.2386081393205242443e-125, 1.1062055705591186799e-141, 1.1734404793201255869e-157,
  1.0451839188820145747e-108, 4.2386081393205242443e-125, 1.1062055705591186799e-141, 1.1734404793201255869e-157,
  1.0451839188820145747e-108, 4.2386081393205242443e-125, 1.1062055705591186799e-141, 1.1734404793201255869e-157,
  1.0451839188820145747e-108, 4.2386081393205242443e-125, 1.1062055705591186799e-141, 1.1734404793201255869e-157,
  1.0451839188820145747e-108, 4.2386081393205242443e-125, 1.1062055705591186799e-141, 1.1734404793201255869e-157,
  1.9359195088038447797e-109, -4.8867691298577234423e-126, -2.0587960670007819622e-142, -2.8326669474241479263e-158,
  1.9359195088038447797e-109, -4.8867691298577234423e-126, -2.0587960670007819622e-142, -2.8326669474241479263e-158,
  1.9359195088038447797e-109, -4.8867691298577234423e-126, -2.0587960670007819622e-142, -2.8326669474241479263e-158,
  8.7142954880180709975e-110, -4.8867691298577234423e-126, -2.0587960670007819622e-142, -2.8326669474241479263e-158,
  3.3918456880078814158e-110, 6.931443500908017045e-126, 1.1062055705591186799e-141, 1.1734404793201255869e-157,
  7.3062078800278780675e-111, 1.0223371855251471293e-126, 1.2214168761472102282e-142, 8.0910098773220312367e-159,
  7.3062078800278780675e-111, 1.0223371855251471293e-126, 1.2214168761472102282e-142, 8.0910098773220312367e-159,
  6.5314563001514349095e-112, 9.9039323746573674262e-128, -8.6629775332868972816e-145, -1.5987060076657616072e-160,
  6.5314563001514349095e-112, 9.9039323746573674262e-128, -8.6629775332868972816e-145, -1.5987060076657616072e-160,
  6.5314563001514349095e-112, 9.9039323746573674262e-128, -8.6629775332868972816e-145, -1.5987060076657616072e-160,
  6.5314563001514349095e-112, 9.9039323746573674262e-128, -8.6629775332868972816e-145, -1.5987060076657616072e-160,
  2.3732923938934761454e-112, 6.7095375687163138915e-129, 1.6963686085056791706e-144, 1.2464251916751375716e-160,
  2.9421044076449630171e-113, 6.7095375687163138915e-129, 1.6963686085056791706e-144, 1.2464251916751375716e-160,
  2.9421044076449630171e-113, 6.7095375687163138915e-129, 1.6963686085056791706e-144, 1.2464251916751375716e-160,
  2.9421044076449630171e-113, 6.7095375687163138915e-129, 1.6963686085056791706e-144, 1.2464251916751375716e-160,
  3.4325196623373878948e-114, 9.3892593260023063019e-130, 9.4702132359198537748e-146, 1.7950099192230045857e-161,
  3.4325196623373878948e-114, 9.3892593260023063019e-130, 9.4702132359198537748e-146, 1.7950099192230045857e-161,
  3.4325196623373878948e-114, 9.3892593260023063019e-130, 9.4702132359198537748e-146, 1.7950099192230045857e-161,
  1.8395411057335783574e-115, -7.8150389500644475446e-132, -3.9681466199873824165e-148, 2.9106774506606945839e-164,
  1.8395411057335783574e-115, -7.8150389500644475446e-132, -3.9681466199873824165e-148, 2.9106774506606945839e-164,
  1.8395411057335783574e-115, -7.8150389500644475446e-132, -3.9681466199873824165e-148, 2.9106774506606945839e-164,
  1.8395411057335783574e-115, -7.8150389500644475446e-132, -3.9681466199873824165e-148, 2.9106774506606945839e-164,
  1.8395411057335783574e-115, -7.8150389500644475446e-132, -3.9681466199873824165e-148, 2.9106774506606945839e-164,
  8.2436437080731844263e-116, 1.4726412753514008951e-131, -3.9681466199873824165e-148, 2.9106774506606945839e-164,
  3.1677600334418871069e-116, 3.4556869017247794521e-132, 8.544872724906996972e-148, 1.6802919634942429241e-163,
  6.2981819612623816536e-117, 6.3800543877747317218e-133, 7.2423563434801054878e-149, 1.1741471776254779927e-164,
  6.2981819612623816536e-117, 6.3800543877747317218e-133, 7.2423563434801054878e-149, 1.1741471776254779927e-164,
  6.2981819612623816536e-117, 6.3800543877747317218e-133, 7.2423563434801054878e-149, 1.1741471776254779927e-164,
  3.1257546646178208289e-117, -6.6414926959353515111e-134, -5.7828074707888119584e-150, -1.2825052715093464343e-165,
  1.5395410162955400644e-117, -6.6414926959353515111e-134, -5.7828074707888119584e-150, -1.2825052715093464343e-165,
  7.4643419213439950602e-118, 1.0969016447485317626e-133, -5.7828074707888119584e-150, -1.2825052715093464343e-165,
  3.4988078005382940294e-118, 2.1637618757749825688e-134, -8.9490928918944555247e-151, -1.9717385086233606481e-166,
  1.5160407401354430737e-118, 2.1637618757749825688e-134, -8.9490928918944555247e-151, -1.9717385086233606481e-166,
  5.2465720993401781599e-119, -3.755176715260116501e-136, 2.1571619860435648643e-152, 6.3257905089784152346e-168,
  2.896544483330507019e-120, 3.1239284188885823808e-136, 2.1571619860435648643e-152, 6.3257905089784152346e-168,
  2.896544483330507019e-120, 3.1239284188885823808e-136, 2.1571619860435648643e-152, 6.3257905089784152346e-168,
  2.896544483330507019e-120, 3.1239284188885823808e-136, 2.1571619860435648643e-152, 6.3257905089784152346e-168,
  2.896544483330507019e-120, 3.1239284188885823808e-136, 2.1571619860435648643e-152, 6.3257905089784152346e-168,
  2.896544483330507019e-120, 3.1239284188885823808e-136, 2.1571619860435648643e-152, 6.3257905089784152346e-168,
  1.3475077173907800538e-120, -3.156241481857667737e-137, -7.0684085473731388916e-153, -3.3573283875161501977e-170,
  5.7298933442091639924e-121, -3.156241481857667737e-137, -7.0684085473731388916e-153, -3.3573283875161501977e-170,
  1.8573014293598452896e-121, 1.1431992269852681095e-137, 2.4782675885631257398e-153, -3.3573283875161501977e-170,
  1.8573014293598452896e-121, 1.1431992269852681095e-137, 2.4782675885631257398e-153, -3.3573283875161501977e-170,
  8.8915345064751572143e-122, 1.1431992269852681095e-137, 2.4782675885631257398e-153, -3.3573283875161501977e-170,
  4.0507946129135104481e-122, 6.8339049774534147855e-139, 9.1598554579059548847e-155, -4.5159745404911825673e-172,
  1.6304246661326865276e-122, 6.8339049774534147855e-139, 9.1598554579059548847e-155, -4.5159745404911825673e-172,
  4.2023969274227456735e-123, 6.8339049774534147855e-139, 9.1598554579059548847e-155, -4.5159745404911825673e-172,
  4.2023969274227456735e-123, 6.8339049774534147855e-139, 9.1598554579059548847e-155, -4.5159745404911825673e-172,
  1.1769344939467164447e-123, 1.1602886988632691941e-140, 3.0307583960570927356e-156, 5.8345524661064369683e-172,
  1.1769344939467164447e-123, 1.1602886988632691941e-140, 3.0307583960570927356e-156, 5.8345524661064369683e-172,
  4.2056888557770896953e-124, 1.1602886988632691941e-140, 3.0307583960570927356e-156, 5.8345524661064369683e-172,
  4.2386081393205242443e-125, 1.1062055705591186799e-141, 1.1734404793201255869e-157, 1.2381024895275844856e-174,
  4.2386081393205242443e-125, 1.1062055705591186799e-141, 1.1734404793201255869e-157, 1.2381024895275844856e-174,
  4.2386081393205242443e-125, 1.1062055705591186799e-141, 1.1734404793201255869e-157, 1.2381024895275844856e-174,
  4.2386081393205242443e-125, 1.1062055705591186799e-141, 1.1734404793201255869e-157, 1.2381024895275844856e-174,
  1.8749656131673758844e-125, 1.1062055705591186799e-141, 1.1734404793201255869e-157, 1.2381024895275844856e-174,
  6.931443500908017045e-126, 1.1062055705591186799e-141, 1.1734404793201255869e-157, 1.2381024895275844856e-174,
  1.0223371855251471293e-126, 1.2214168761472102282e-142, 8.0910098773220302259e-159, 1.2381024895275844856e-174,
  1.0223371855251471293e-126, 1.2214168761472102282e-142, 8.0910098773220302259e-159, 1.2381024895275844856e-174,
  1.0223371855251471293e-126, 1.2214168761472102282e-142, 8.0910098773220302259e-159, 1.2381024895275844856e-174,
  2.8369889610228834887e-127, 4.0136364036021218058e-143, -1.0134099605688458828e-159, -2.5389576707476506925e-176,
  2.8369889610228834887e-127, 4.0136364036021218058e-143, -1.0134099605688458828e-159, -2.5389576707476506925e-176,
  9.9039323746573674262e-128, -8.6629775332868972816e-145, -1.5987060076657612913e-160, -2.5389576707476506925e-176,
  6.7095375687163138915e-129, 1.6963686085056791706e-144, 1.2464251916751375716e-160, 6.197724948400014906e-177,
  6.7095375687163138915e-129, 1.6963686085056791706e-144, 1.2464251916751375716e-160, 6.197724948400014906e-177,
  6.7095375687163138915e-129, 1.6963686085056791706e-144, 1.2464251916751375716e-160, 6.197724948400014906e-177,
  6.7095375687163138915e-129, 1.6963686085056791706e-144, 1.2464251916751375716e-160, 6.197724948400014906e-177,
  9.3892593260023063019e-130, 9.4702132359198537748e-146, 1.7950099192230045857e-161, -1.6991004655691155518e-177,
  9.3892593260023063019e-130, 9.4702132359198537748e-146, 1.7950099192230045857e-161, -1.6991004655691155518e-177,
  9.3892593260023063019e-130, 9.4702132359198537748e-146, 1.7950099192230045857e-161, -1.6991004655691155518e-177,
  2.175994780857201024e-130, 1.4618808551874518553e-146, 1.6802919634942426156e-163, 2.8330093736631818036e-179,
  2.175994780857201024e-130, 1.4618808551874518553e-146, 1.6802919634942426156e-163, 2.8330093736631818036e-179,
  3.7267864457092460442e-131, 4.6083930759590139305e-147, 1.6802919634942426156e-163, 2.8330093736631818036e-179,
  3.7267864457092460442e-131, 4.6083930759590139305e-147, 1.6802919634942426156e-163, 2.8330093736631818036e-179,
  3.7267864457092460442e-131, 4.6083930759590139305e-147, 1.6802919634942426156e-163, 2.8330093736631818036e-179,
  1.4726412753514008951e-131, -3.9681466199873824165e-148, 2.9106774506606941983e-164, 5.1948630316441296498e-180,
  3.4556869017247794521e-132, 8.544872724906996972e-148, 1.6802919634942426156e-163, 2.8330093736631818036e-179,
  3.4556869017247794521e-132, 8.544872724906996972e-148, 1.6802919634942426156e-163, 2.8330093736631818036e-179,
  6.3800543877747317218e-133, 7.2423563434801054878e-149, 1.1741471776254777999e-164, 1.3389912474795152755e-180,
  6.3800543877747317218e-133, 7.2423563434801054878e-149, 1.1741471776254777999e-164, 1.3389912474795152755e-180,
  6.3800543877747317218e-133, 7.2423563434801054878e-149, 1.1741471776254777999e-164, 1.3389912474795152755e-180,
  2.8579525590905986764e-133, -5.7828074707888119584e-150, -1.2825052715093464343e-165, -1.0696067158221530218e-181,
  1.0969016447485317626e-133, -5.7828074707888119584e-150, -1.2825052715093464343e-165, -1.0696067158221530218e-181,
  2.1637618757749825688e-134, -8.9490928918944555247e-151, -1.9717385086233606481e-166, 1.3535321672928907047e-182,
  2.1637618757749825688e-134, -8.9490928918944555247e-151, -1.9717385086233606481e-166, 1.3535321672928907047e-182,
  2.1637618757749825688e-134, -8.9490928918944555247e-151, -1.9717385086233606481e-166, 1.3535321672928907047e-182,
  1.0631050543111905033e-134, 1.5490398016102376505e-150, 3.4549185946116918017e-166, 1.3535321672928907047e-182,
  5.1277664357929471499e-135, 3.2706525621039604902e-151, 7.4159004299416557678e-167, 1.3535321672928907047e-182,
  2.3761243821334675971e-135, 3.2706525621039604902e-151, 7.4159004299416557678e-167, 1.3535321672928907047e-182,
  1.0003033553037281263e-135, 2.1571619860435648643e-152, 6.3257905089784152346e-168, 3.5607241064750984115e-184,
  3.1239284188885823808e-136, 2.1571619860435648643e-152, 6.3257905089784152346e-168, 3.5607241064750984115e-184,
  3.1239284188885823808e-136, 2.1571619860435648643e-152, 6.3257905089784152346e-168, 3.5607241064750984115e-184,
  1.4041521353514076604e-136, 2.1571619860435648643e-152, 6.3257905089784152346e-168, 3.5607241064750984115e-184,
  5.4426399358282049106e-137, 2.4782675885631257398e-153, -3.3573283875161501977e-170, 3.0568054078295488291e-186,
  1.1431992269852681095e-137, 2.4782675885631257398e-153, -3.3573283875161501977e-170, 3.0568054078295488291e-186,
  1.1431992269852681095e-137, 2.4782675885631257398e-153, -3.3573283875161501977e-170, 3.0568054078295488291e-186,
  6.8339049774534147855e-139, 9.1598554579059548847e-155, -4.5159745404911819927e-172, -4.5870810097328578981e-188,
  6.8339049774534147855e-139, 9.1598554579059548847e-155, -4.5159745404911819927e-172, -4.5870810097328578981e-188,
  6.8339049774534147855e-139, 9.1598554579059548847e-155, -4.5159745404911819927e-172, -4.5870810097328578981e-188,
  6.8339049774534147855e-139, 9.1598554579059548847e-155, -4.5159745404911819927e-172, -4.5870810097328578981e-188,
  1.1602886988632691941e-140, 3.0307583960570927356e-156, 5.8345524661064358191e-172, 6.9043123899963188689e-188,
  1.1602886988632691941e-140, 3.0307583960570927356e-156, 5.8345524661064358191e-172, 6.9043123899963188689e-188,
  1.1602886988632691941e-140, 3.0307583960570927356e-156, 5.8345524661064358191e-172, 6.9043123899963188689e-188,
  1.1602886988632691941e-140, 3.0307583960570927356e-156, 5.8345524661064358191e-172, 6.9043123899963188689e-188,
  1.1602886988632691941e-140, 3.0307583960570927356e-156, 5.8345524661064358191e-172, 6.9043123899963188689e-188,
  1.1602886988632691941e-140, 3.0307583960570927356e-156, 5.8345524661064358191e-172, 6.9043123899963188689e-188,
  1.1062055705591186799e-141, 1.1734404793201255869e-157, 1.2381024895275844856e-174, -8.4789520282639751913e-191,
  1.1062055705591186799e-141, 1.1734404793201255869e-157, 1.2381024895275844856e-174, -8.4789520282639751913e-191,
  1.1062055705591186799e-141, 1.1734404793201255869e-157, 1.2381024895275844856e-174, -8.4789520282639751913e-191,
  1.1062055705591186799e-141, 1.1734404793201255869e-157, 1.2381024895275844856e-174, -8.4789520282639751913e-191,
  4.5016298192952031469e-142, -2.8326669474241479263e-158, 1.2381024895275844856e-174, -8.4789520282639751913e-191,
  1.2214168761472102282e-142, 8.0910098773220302259e-159, 1.2381024895275844856e-174, -8.4789520282639751913e-191,
  1.2214168761472102282e-142, 8.0910098773220302259e-159, 1.2381024895275844856e-174, -8.4789520282639751913e-191,
  4.0136364036021218058e-143, -1.0134099605688458828e-159, -2.5389576707476506925e-176, -6.2404128071707654958e-193,
  4.0136364036021218058e-143, -1.0134099605688458828e-159, -2.5389576707476506925e-176, -6.2404128071707654958e-193,
  1.9635033141346264592e-143, -1.0134099605688458828e-159, -2.5389576707476506925e-176, -6.2404128071707654958e-193,
  9.3843676940087855824e-144, 1.2626949989038732076e-159, 2.2730883653953564668e-175, 2.7431118386590483722e-191,
  4.2590349703400483539e-144, 1.2464251916751375716e-160, 6.1977249484000140293e-177, 1.1294061984896458822e-192,
  1.6963686085056791706e-144, 1.2464251916751375716e-160, 6.1977249484000140293e-177, 1.1294061984896458822e-192,
  4.1503542758849472122e-145, -1.7614040799531193879e-161, -1.6991004655691153326e-177, -1.856794109153959173e-193,
  4.1503542758849472122e-145, -1.7614040799531193879e-161, -1.6991004655691153326e-177, -1.856794109153959173e-193,
  9.4702132359198537748e-146, 1.7950099192230045857e-161, -1.6991004655691153326e-177, -1.856794109153959173e-193,
  9.4702132359198537748e-146, 1.7950099192230045857e-161, -1.6991004655691153326e-177, -1.856794109153959173e-193,
  1.4618808551874518553e-146, 1.6802919634942426156e-163, 2.8330093736631818036e-179, -7.4549709281190454638e-196,
  1.4618808551874518553e-146, 1.6802919634942426156e-163, 2.8330093736631818036e-179, -7.4549709281190454638e-196,
  1.4618808551874518553e-146, 1.6802919634942426156e-163, 2.8330093736631818036e-179, -7.4549709281190454638e-196,
  4.6083930759590139305e-147, 1.6802919634942426156e-163, 2.8330093736631818036e-179, -7.4549709281190454638e-196,
  4.6083930759590139305e-147, 1.6802919634942426156e-163, 2.8330093736631818036e-179, -7.4549709281190454638e-196,
  2.105789206980137775e-147, 1.6802919634942426156e-163, 2.8330093736631818036e-179, -7.4549709281190454638e-196,
  8.544872724906996972e-148, 1.6802919634942426156e-163, 2.8330093736631818036e-179, -7.4549709281190454638e-196,
  2.2883630524598079723e-148, 2.9106774506606941983e-164, 5.1948630316441287936e-180, 9.6685396110091032843e-196,
  2.2883630524598079723e-148, 2.9106774506606941983e-164, 5.1948630316441287936e-180, 9.6685396110091032843e-196,
  7.2423563434801054878e-149, 1.1741471776254777999e-164, 1.3389912474795150614e-180, 1.1067843414450286726e-196,
  7.2423563434801054878e-149, 1.1741471776254777999e-164, 1.3389912474795150614e-180, 1.1067843414450286726e-196,
  3.3320377982006123631e-149, 3.0588204110786950436e-165, 3.7502330143836152136e-181, 3.6564932749519464998e-198,
  1.3768785255608653665e-149, 3.0588204110786950436e-165, 3.7502330143836152136e-181, 3.6564932749519464998e-198,
  3.9929888924099219388e-150, -1.9717385086233606481e-166, 1.3535321672928907047e-182, 3.1205762277848031878e-199,
  3.9929888924099219388e-150, -1.9717385086233606481e-166, 1.3535321672928907047e-182, 3.1205762277848031878e-199,
  1.5490398016102376505e-150, 3.4549185946116918017e-166, 1.3535321672928907047e-182, 3.1205762277848031878e-199,
  3.2706525621039604902e-151, 7.4159004299416557678e-167, 1.3535321672928907047e-182, 3.1205762277848031878e-199,
  3.2706525621039604902e-151, 7.4159004299416557678e-167, 1.3535321672928907047e-182, 3.1205762277848031878e-199,
  2.1571619860435648643e-152, 6.3257905089784152346e-168, 3.5607241064750984115e-184, -1.4832196127821708615e-201,
  2.1571619860435648643e-152, 6.3257905089784152346e-168, 3.5607241064750984115e-184, -1.4832196127821708615e-201,
  2.1571619860435648643e-152, 6.3257905089784152346e-168, 3.5607241064750984115e-184, -1.4832196127821708615e-201,
  2.1571619860435648643e-152, 6.3257905089784152346e-168, 3.5607241064750984115e-184, -1.4832196127821708615e-201,
  2.4782675885631257398e-153, -3.3573283875161501977e-170, 3.0568054078295488291e-186, 1.4980560800565462618e-202,
  2.4782675885631257398e-153, -3.3573283875161501977e-170, 3.0568054078295488291e-186, 1.4980560800565462618e-202,
  2.4782675885631257398e-153, -3.3573283875161501977e-170, 3.0568054078295488291e-186, 1.4980560800565462618e-202,
  9.1598554579059548847e-155, -4.5159745404911819927e-172, -4.5870810097328572602e-188, -3.2905064432040069127e-204,
  9.1598554579059548847e-155, -4.5159745404911819927e-172, -4.5870810097328572602e-188, -3.2905064432040069127e-204,
  9.1598554579059548847e-155, -4.5159745404911819927e-172, -4.5870810097328572602e-188, -3.2905064432040069127e-204,
  9.1598554579059548847e-155, -4.5159745404911819927e-172, -4.5870810097328572602e-188, -3.2905064432040069127e-204,
  9.1598554579059548847e-155, -4.5159745404911819927e-172, -4.5870810097328572602e-188, -3.2905064432040069127e-204,
  1.7015147267057481414e-155, -4.5159745404911819927e-172, -4.5870810097328572602e-188, -3.2905064432040069127e-204,
  1.7015147267057481414e-155, -4.5159745404911819927e-172, -4.5870810097328572602e-188, -3.2905064432040069127e-204,
  1.7015147267057481414e-155, -4.5159745404911819927e-172, -4.5870810097328572602e-188, -3.2905064432040069127e-204,
  7.6922213530572229852e-156, -4.5159745404911819927e-172, -4.5870810097328572602e-188, -3.2905064432040069127e-204,
  3.0307583960570927356e-156, 5.8345524661064358191e-172, 6.9043123899963188689e-188, -3.2905064432040069127e-204,
  7.0002691755702864582e-157, 6.5928896280762691321e-173, 1.1586156901317304854e-188, -1.0100405885278530137e-205,
  7.0002691755702864582e-157, 6.5928896280762691321e-173, 1.1586156901317304854e-188, -1.0100405885278530137e-205,
  1.1734404793201255869e-157, 1.2381024895275844856e-174, -8.4789520282639751913e-191, -1.3321093418096261919e-207,
  1.1734404793201255869e-157, 1.2381024895275844856e-174, -8.4789520282639751913e-191, -1.3321093418096261919e-207,
  1.1734404793201255869e-157, 1.2381024895275844856e-174, -8.4789520282639751913e-191, -1.3321093418096261919e-207,
  4.4508689228885539715e-158, 1.2381024895275844856e-174, -8.4789520282639751913e-191, -1.3321093418096261919e-207,
  8.0910098773220302259e-159, 1.2381024895275844856e-174, -8.4789520282639751913e-191, -1.3321093418096261919e-207,
  8.0910098773220302259e-159, 1.2381024895275844856e-174, -8.4789520282639751913e-191, -1.3321093418096261919e-207,
  8.0910098773220302259e-159, 1.2381024895275844856e-174, -8.4789520282639751913e-191, -1.3321093418096261919e-207,
  3.5387999583765925506e-159, 2.2730883653953564668e-175, 2.7431118386590483722e-191, -1.3321093418096261919e-207,
  1.2626949989038732076e-159, 2.2730883653953564668e-175, 2.7431118386590483722e-191, -1.3321093418096261919e-207,
  1.2464251916751375716e-160, 6.1977249484000140293e-177, 1.1294061984896456875e-192, 2.2526486929936882202e-208,
  1.2464251916751375716e-160, 6.1977249484000140293e-177, 1.1294061984896456875e-192, 2.2526486929936882202e-208,
  1.2464251916751375716e-160, 6.1977249484000140293e-177, 1.1294061984896456875e-192, 2.2526486929936882202e-208,
  1.2464251916751375716e-160, 6.1977249484000140293e-177, 1.1294061984896456875e-192, 2.2526486929936882202e-208,
  5.3514239183991277695e-161, 6.1977249484000140293e-177, 1.1294061984896456875e-192, 2.2526486929936882202e-208,
  1.7950099192230045857e-161, -1.6991004655691153326e-177, -1.8567941091539589297e-193, -1.8074851186411640793e-209,
  1.6802919634942426156e-163, 2.8330093736631818036e-179, -7.4549709281190454638e-196, -1.4481306607622412036e-212,
  1.6802919634942426156e-163, 2.8330093736631818036e-179, -7.4549709281190454638e-196, -1.4481306607622412036e-212,
  1.6802919634942426156e-163, 2.8330093736631818036e-179, -7.4549709281190454638e-196, -1.4481306607622412036e-212,
  1.6802919634942426156e-163, 2.8330093736631818036e-179, -7.4549709281190454638e-196, -1.4481306607622412036e-212,
  1.6802919634942426156e-163, 2.8330093736631818036e-179, -7.4549709281190454638e-196, -1.4481306607622412036e-212,
  1.6802919634942426156e-163, 2.8330093736631818036e-179, -7.4549709281190454638e-196, -1.4481306607622412036e-212,
  1.6802919634942426156e-163, 2.8330093736631818036e-179, -7.4549709281190454638e-196, -1.4481306607622412036e-212,
  2.9106774506606941983e-164, 5.1948630316441287936e-180, 9.6685396110091013832e-196, 1.7562785002189357559e-211,
  2.9106774506606941983e-164, 5.1948630316441287936e-180, 9.6685396110091013832e-196, 1.7562785002189357559e-211,
  2.9106774506606941983e-164, 5.1948630316441287936e-180, 9.6685396110091013832e-196, 1.7562785002189357559e-211,
  1.1741471776254777999e-164, 1.3389912474795150614e-180, 1.106784341445028435e-196, 3.3045982549756583552e-212,
  3.0588204110786950436e-165, 3.7502330143836152136e-181, 3.6564932749519464998e-198, 3.7097125405852507464e-214,
  3.0588204110786950436e-165, 3.7502330143836152136e-181, 3.6564932749519464998e-198, 3.7097125405852507464e-214,
  8.8815756978467430465e-166, 1.3403131492807310959e-181, 3.6564932749519464998e-198, 3.7097125405852507464e-214,
  8.8815756978467430465e-166, 1.3403131492807310959e-181, 3.6564932749519464998e-198, 3.7097125405852507464e-214,
  3.4549185946116918017e-166, 1.3535321672928907047e-182, 3.1205762277848031878e-199, -3.3569248349832580936e-217,
  7.4159004299416557678e-167, 1.3535321672928907047e-182, 3.1205762277848031878e-199, -3.3569248349832580936e-217,
  7.4159004299416557678e-167, 1.3535321672928907047e-182, 3.1205762277848031878e-199, -3.3569248349832580936e-217,
  6.3257905089784152346e-168, 3.5607241064750984115e-184, -1.4832196127821708615e-201, 2.6911956484118910092e-218,
  6.3257905089784152346e-168, 3.5607241064750984115e-184, -1.4832196127821708615e-201, 2.6911956484118910092e-218,
  6.3257905089784152346e-168, 3.5607241064750984115e-184, -1.4832196127821708615e-201, 2.6911956484118910092e-218,
  6.3257905089784152346e-168, 3.5607241064750984115e-184, -1.4832196127821708615e-201, 2.6911956484118910092e-218,
  2.0862146470760309789e-168, -1.146150630053972131e-184, -1.4832196127821708615e-201, 2.6911956484118910092e-218,
  2.0862146470760309789e-168, -1.146150630053972131e-184, -1.4832196127821708615e-201, 2.6911956484118910092e-218,
  1.026320681600434562e-168, 1.2072867382105631402e-184, -1.4832196127821708615e-201, 2.6911956484118910092e-218,
  4.9637369886263658882e-169, 3.0568054078295488291e-186, 1.4980560800565460352e-202, 2.6911956484118910092e-218,
  2.3140020749373754342e-169, 3.0568054078295488291e-186, 1.4980560800565460352e-202, 2.6911956484118910092e-218,
  9.8913461809288020723e-170, 3.0568054078295488291e-186, 1.4980560800565460352e-202, 2.6911956484118910092e-218,
  3.2670088967063259373e-170, 3.0568054078295488291e-186, 1.4980560800565460352e-202, 2.6911956484118910092e-218,
  3.2670088967063259373e-170, 3.0568054078295488291e-186, 1.4980560800565460352e-202, 2.6911956484118910092e-218,
  1.6109245756507072713e-170, -6.2044048008378732802e-187, -5.4322544592823556944e-203, 4.2491789852161138683e-219,
  7.8288241512289757055e-171, 1.2181824638728806485e-186, 1.4980560800565460352e-202, 2.6911956484118910092e-218,
  3.6886133485899290404e-171, 2.9887099189454666024e-187, 4.774153170641553462e-203, 4.2491789852161138683e-219,
  1.6185079472704052482e-171, 2.9887099189454666024e-187, 4.774153170641553462e-203, 4.2491789852161138683e-219,
  5.8345524661064358191e-172, 6.9043123899963188689e-188, -3.2905064432040069127e-204, -9.1795828160190082842e-224,
  6.5928896280762691321e-173, 1.1586156901317304854e-188, -1.0100405885278530137e-205, -9.1795828160190082842e-224,
  6.5928896280762691321e-173, 1.1586156901317304854e-188, -1.0100405885278530137e-205, -9.1795828160190082842e-224,
  6.5928896280762691321e-173, 1.1586156901317304854e-188, -1.0100405885278530137e-205, -9.1795828160190082842e-224,
  1.2381024895275844856e-174, -8.4789520282639751913e-191, -1.332109341809626019e-207, -9.1795828160190082842e-224,
  1.2381024895275844856e-174, -8.4789520282639751913e-191, -1.332109341809626019e-207, -9.1795828160190082842e-224,
  1.2381024895275844856e-174, -8.4789520282639751913e-191, -1.332109341809626019e-207, -9.1795828160190082842e-224,
  1.2381024895275844856e-174, -8.4789520282639751913e-191, -1.332109341809626019e-207, -9.1795828160190082842e-224,
  1.2381024895275844856e-174, -8.4789520282639751913e-191, -1.332109341809626019e-207, -9.1795828160190082842e-224,
  1.2381024895275844856e-174, -8.4789520282639751913e-191, -1.332109341809626019e-207, -9.1795828160190082842e-224,
  2.2730883653953564668e-175, 2.7431118386590483722e-191, -1.332109341809626019e-207, -9.1795828160190082842e-224,
  2.2730883653953564668e-175, 2.7431118386590483722e-191, -1.332109341809626019e-207, -9.1795828160190082842e-224,
  2.2730883653953564668e-175, 2.7431118386590483722e-191, -1.332109341809626019e-207, -9.1795828160190082842e-224,
  1.0095962991602958391e-175, -6.2404128071707654958e-193, 3.0593092910744445285e-209, 5.4622616159087170031e-225,
  3.7785026604276538491e-176, -6.2404128071707654958e-193, 3.0593092910744445285e-209, 5.4622616159087170031e-225,
  6.1977249484000140293e-177, 1.1294061984896456875e-192, 2.2526486929936882202e-208, -5.3441928036578162463e-225,
  6.1977249484000140293e-177, 1.1294061984896456875e-192, 2.2526486929936882202e-208, -5.3441928036578162463e-225,
  6.1977249484000140293e-177, 1.1294061984896456875e-192, 2.2526486929936882202e-208, -5.3441928036578162463e-225,
  2.2493122414154495675e-177, 2.5268245888628466632e-193, 3.0593092910744445285e-209, 5.4622616159087170031e-225,
  2.7510588792316711745e-178, 3.3501523985444386676e-194, 6.2591208621664049475e-210, 5.9034406125450500218e-227,
  2.7510588792316711745e-178, 3.3501523985444386676e-194, 6.2591208621664049475e-210, 5.9034406125450500218e-227,
  2.7510588792316711745e-178, 3.3501523985444386676e-194, 6.2591208621664049475e-210, 5.9034406125450500218e-227,
  2.8330093736631818036e-179, -7.4549709281190454638e-196, -1.4481306607622412036e-212, 9.9192633285681635836e-229,
  2.8330093736631818036e-179, -7.4549709281190454638e-196, -1.4481306607622412036e-212, 9.9192633285681635836e-229,
  2.8330093736631818036e-179, -7.4549709281190454638e-196, -1.4481306607622412036e-212, 9.9192633285681635836e-229,
  2.8330093736631818036e-179, -7.4549709281190454638e-196, -1.4481306607622412036e-212, 9.9192633285681635836e-229,
  1.2906606599973359683e-179, -7.4549709281190454638e-196, -1.4481306607622412036e-212, 9.9192633285681635836e-229,
  5.1948630316441287936e-180, 9.6685396110091013832e-196, 1.7562785002189355449e-211, 1.6821693549018732055e-227,
  1.3389912474795150614e-180, 1.106784341445028435e-196, 3.3045982549756578275e-212, 6.2685154049107876715e-228,
  1.3389912474795150614e-180, 1.106784341445028435e-196, 3.3045982549756578275e-212, 6.2685154049107876715e-228,
  3.7502330143836152136e-181, 3.6564932749519464998e-198, 3.7097125405852507464e-214, 2.5658818466966882188e-231,
  3.7502330143836152136e-181, 3.6564932749519464998e-198, 3.7097125405852507464e-214, 2.5658818466966882188e-231,
  1.3403131492807310959e-181, 3.6564932749519464998e-198, 3.7097125405852507464e-214, 2.5658818466966882188e-231,
  1.3535321672928907047e-182, 3.1205762277848031878e-199, -3.3569248349832580936e-217, -1.0577661142165146927e-233,
  1.3535321672928907047e-182, 3.1205762277848031878e-199, -3.3569248349832580936e-217, -1.0577661142165146927e-233,
  1.3535321672928907047e-182, 3.1205762277848031878e-199, -3.3569248349832580936e-217, -1.0577661142165146927e-233,
  1.3535321672928907047e-182, 3.1205762277848031878e-199, -3.3569248349832580936e-217, -1.0577661142165146927e-233,
  6.0043220944823941786e-183, 3.1205762277848031878e-199, -3.3569248349832580936e-217, -1.0577661142165146927e-233,
  2.2388223052591377446e-183, 3.1205762277848031878e-199, -3.3569248349832580936e-217, -1.0577661142165146927e-233,
  3.5607241064750984115e-184, -1.4832196127821708615e-201, 2.6911956484118910092e-218, -5.1336618966962585332e-235,
  3.5607241064750984115e-184, -1.4832196127821708615e-201, 2.6911956484118910092e-218, -5.1336618966962585332e-235,
  3.5607241064750984115e-184, -1.4832196127821708615e-201, 2.6911956484118910092e-218, -5.1336618966962585332e-235,
  1.2072867382105631402e-184, -1.4832196127821708615e-201, 2.6911956484118910092e-218, -5.1336618966962585332e-235,
  3.0568054078295488291e-186, 1.4980560800565460352e-202, 2.6911956484118910092e-218, -5.1336618966962585332e-235,
  3.0568054078295488291e-186, 1.4980560800565460352e-202, 2.6911956484118910092e-218, -5.1336618966962585332e-235,
  3.0568054078295488291e-186, 1.4980560800565460352e-202, 2.6911956484118910092e-218, -5.1336618966962585332e-235,
  3.0568054078295488291e-186, 1.4980560800565460352e-202, 2.6911956484118910092e-218, -5.1336618966962585332e-235,
  3.0568054078295488291e-186, 1.4980560800565460352e-202, 2.6911956484118910092e-218, -5.1336618966962585332e-235,
  3.0568054078295488291e-186, 1.4980560800565460352e-202, 2.6911956484118910092e-218, -5.1336618966962585332e-235,
  1.2181824638728806485e-186, 1.4980560800565460352e-202, 2.6911956484118910092e-218, -5.1336618966962585332e-235,
  2.9887099189454666024e-187, 4.774153170641553462e-203, 4.2491789852161132393e-219, 7.4467067939231424594e-235,
  2.9887099189454666024e-187, 4.774153170641553462e-203, 4.2491789852161132393e-219, 7.4467067939231424594e-235,
  6.9043123899963188689e-188, -3.2905064432040069127e-204, -9.1795828160190063645e-224, -2.3569545504732004486e-239,
  6.9043123899963188689e-188, -3.2905064432040069127e-204, -9.1795828160190063645e-224, -2.3569545504732004486e-239,
  1.1586156901317304854e-188, -1.0100405885278530137e-205, -9.1795828160190063645e-224, -2.3569545504732004486e-239,
  1.1586156901317304854e-188, -1.0100405885278530137e-205, -9.1795828160190063645e-224, -2.3569545504732004486e-239,
  1.1586156901317304854e-188, -1.0100405885278530137e-205, -9.1795828160190063645e-224, -2.3569545504732004486e-239,
  4.4040360264865697732e-189, -1.0100405885278530137e-205, -9.1795828160190063645e-224, -2.3569545504732004486e-239,
  8.129755890712020335e-190, 9.8339840169166049336e-206, -9.1795828160190063645e-224, -2.3569545504732004486e-239,
  8.129755890712020335e-190, 9.8339840169166049336e-206, -9.1795828160190063645e-224, -2.3569545504732004486e-239,
  8.129755890712020335e-190, 9.8339840169166049336e-206, -9.1795828160190063645e-224, -2.3569545504732004486e-239,
  3.6409303439428119063e-190, -1.332109341809626019e-207, -9.1795828160190063645e-224, -2.3569545504732004486e-239,
  1.3965175705582071936e-190, -1.332109341809626019e-207, -9.1795828160190063645e-224, -2.3569545504732004486e-239,
  2.7431118386590483722e-191, -1.332109341809626019e-207, -9.1795828160190063645e-224, -2.3569545504732004486e-239,
  2.7431118386590483722e-191, -1.332109341809626019e-207, -9.1795828160190063645e-224, -2.3569545504732004486e-239,
  2.7431118386590483722e-191, -1.332109341809626019e-207, -9.1795828160190063645e-224, -2.3569545504732004486e-239,
  1.3403538552936701153e-191, 1.7826390804083638359e-207, -9.1795828160190063645e-224, -2.3569545504732004486e-239,
  6.389748636109812983e-192, 2.2526486929936882202e-208, -5.3441928036578156465e-225, -7.741539335184153052e-241,
  2.8828536776963681193e-192, 2.2526486929936882202e-208, -5.3441928036578156465e-225, -7.741539335184153052e-241,
  1.1294061984896456875e-192, 2.2526486929936882202e-208, -5.3441928036578156465e-225, -7.741539335184153052e-241,
  2.5268245888628466632e-193, 3.0593092910744445285e-209, 5.4622616159087170031e-225, 4.2560351759808952526e-241,
  2.5268245888628466632e-193, 3.0593092910744445285e-209, 5.4622616159087170031e-225, 4.2560351759808952526e-241,
  3.3501523985444386676e-194, 6.2591208621664049475e-210, 5.9034406125450490845e-227, 1.3186893776791012681e-242,
  3.3501523985444386676e-194, 6.2591208621664049475e-210, 5.9034406125450490845e-227, 1.3186893776791012681e-242,
  3.3501523985444386676e-194, 6.2591208621664049475e-210, 5.9034406125450490845e-227, 1.3186893776791012681e-242,
  6.1039071228393547627e-195, 1.7562785002189355449e-211, 1.6821693549018732055e-227, -8.7276385348052817035e-244,
  6.1039071228393547627e-195, 1.7562785002189355449e-211, 1.6821693549018732055e-227, -8.7276385348052817035e-244,
  6.1039071228393547627e-195, 1.7562785002189355449e-211, 1.6821693549018732055e-227, -8.7276385348052817035e-244,
  2.6792050150137250131e-195, 1.7562785002189355449e-211, 1.6821693549018732055e-227, -8.7276385348052817035e-244,
  9.6685396110091013832e-196, 1.7562785002189355449e-211, 1.6821693549018732055e-227, -8.7276385348052817035e-244,
  2.0416567491425607157e-177, 6.0959078275963141821e-193, 1.156336993964950812e-208, 2.7126166236326293347e-224,
  2.0416567491425607157e-177, 6.0959078275963141821e-193, 1.156336993964950812e-208, 2.7126166236326293347e-224,
  2.0416567491425607157e-177, 6.0959078275963141821e-193, 1.156336993964950812e-208, 2.7126166236326293347e-224,
  6.7450395650278649168e-179, 6.8432117823206978686e-195, 4.7332165749391048364e-212, 4.4984059688774601837e-228,
  6.7450395650278649168e-179, 6.8432117823206978686e-195, 4.7332165749391048364e-212, 4.4984059688774601837e-228,
  6.7450395650278649168e-179, 6.8432117823206978686e-195, 4.7332165749391048364e-212, 4.4984059688774601837e-228,
  6.7450395650278649168e-179, 6.8432117823206978686e-195, 4.7332165749391048364e-212, 4.4984059688774601837e-228,
  6.7450395650278649168e-179, 6.8432117823206978686e-195, 4.7332165749391048364e-212, 4.4984059688774601837e-228,
  5.756447103644822603e-180, -6.1924333305615830735e-198, -1.9512340798794268979e-214, -3.6162764918921697356e-230,
  5.756447103644822603e-180, -6.1924333305615830735e-198, -1.9512340798794268979e-214, -3.6162764918921697356e-230,
  5.756447103644822603e-180, -6.1924333305615830735e-198, -1.9512340798794268979e-214, -3.6162764918921697356e-230,
  5.756447103644822603e-180, -6.1924333305615830735e-198, -1.9512340798794268979e-214, -3.6162764918921697356e-230,
  1.9005753194802080146e-180, -6.1924333305615830735e-198, -1.9512340798794268979e-214, -3.6162764918921697356e-230,
  1.9005753194802080146e-180, -6.1924333305615830735e-198, -1.9512340798794268979e-214, -3.6162764918921697356e-230,
  9.3660737343905436753e-181, -6.1924333305615830735e-198, -1.9512340798794268979e-214, -3.6162764918921697356e-230,
  4.5462340041847754398e-181, -6.1924333305615830735e-198, -1.9512340798794268979e-214, -3.6162764918921697356e-230,
  2.1363141390818913221e-181, -6.1924333305615830735e-198, -1.9512340798794268979e-214, -3.6162764918921697356e-230,
  9.3135420653044926323e-182, -6.1924333305615830735e-198, -1.9512340798794268979e-214, -3.6162764918921697356e-230,
  3.2887424025472810002e-182, 7.185309278132283136e-198, -1.9512340798794268979e-214, -3.6162764918921697356e-230,
  2.7634257116867652192e-183, 4.9643797378534984559e-199, -9.4699347169310243473e-216, -9.2331809177749095611e-233,
  2.7634257116867652192e-183, 4.9643797378534984559e-199, -9.4699347169310243473e-216, -9.2331809177749095611e-233,
  2.7634257116867652192e-183, 4.9643797378534984559e-199, -9.4699347169310243473e-216, -9.2331809177749095611e-233,
  2.7634257116867652192e-183, 4.9643797378534984559e-199, -9.4699347169310243473e-216, -9.2331809177749095611e-233,
  8.806758170751374203e-184, 7.8383517263666503337e-200, 1.3736749441945438342e-215, -9.2331809177749095611e-233,
  8.806758170751374203e-184, 7.8383517263666503337e-200, 1.3736749441945438342e-215, -9.2331809177749095611e-233,
  4.0998834342223036605e-184, 7.8383517263666503337e-200, 1.3736749441945438342e-215, -9.2331809177749095611e-233,
  1.7464460659577689118e-184, 2.612671019845610006e-200, 2.1334073625072069974e-216, -9.2331809177749095611e-233,
  5.697273818255015375e-185, -1.6933341491052464293e-204, -4.3478137385944270631e-220, -2.3353910329236990725e-236,
  5.697273818255015375e-185, -1.6933341491052464293e-204, -4.3478137385944270631e-220, -2.3353910329236990725e-236,
  2.755477107924346286e-185, -1.6933341491052464293e-204, -4.3478137385944270631e-220, -2.3353910329236990725e-236,
  1.2845787527590117414e-185, -1.6933341491052464293e-204, -4.3478137385944270631e-220, -2.3353910329236990725e-236,
  5.4912957517634446918e-186, -1.6933341491052464293e-204, -4.3478137385944270631e-220, -2.3353910329236990725e-236,
  1.8140498638501083305e-186, -1.6933341491052464293e-204, -4.3478137385944270631e-220, -2.3353910329236990725e-236,
  1.8140498638501083305e-186, -1.6933341491052464293e-204, -4.3478137385944270631e-220, -2.3353910329236990725e-236,
  8.9473839187177424013e-187, -1.6933341491052464293e-204, -4.3478137385944270631e-220, -2.3353910329236990725e-236,
  4.3508265588260719497e-187, -1.6933341491052464293e-204, -4.3478137385944270631e-220, -2.3353910329236990725e-236,
  2.0525478788802367239e-187, -1.6933341491052464293e-204, -4.3478137385944270631e-220, -2.3353910329236990725e-236,
  9.0340853890731911095e-188, -1.6933341491052464293e-204, -4.3478137385944270631e-220, -2.3353910329236990725e-236,
  3.288388689208603045e-188, -1.6933341491052464293e-204, -4.3478137385944270631e-220, -2.3353910329236990725e-236,
  4.1554033927630885323e-189, -9.8582956929636044137e-206, -1.4280619485269765742e-221, 1.2171222696290252021e-237,
  4.1554033927630885323e-189, -9.8582956929636044137e-206, -1.4280619485269765742e-221, 1.2171222696290252021e-237,
  4.1554033927630885323e-189, -9.8582956929636044137e-206, -1.4280619485269765742e-221, 1.2171222696290252021e-237,
  5.643429553477207926e-190, 1.0076094209231528444e-205, 7.8509991660024955813e-222, 1.2171222696290252021e-237,
  5.643429553477207926e-190, 1.0076094209231528444e-205, 7.8509991660024955813e-222, 1.2171222696290252021e-237,
  5.643429553477207926e-190, 1.0076094209231528444e-205, 7.8509991660024955813e-222, 1.2171222696290252021e-237,
  1.1546040067079994973e-190, 1.0889925813396166947e-207, 2.4325525462765697993e-223, -1.1429360314275701698e-239,
  1.1546040067079994973e-190, 1.0889925813396166947e-207, 2.4325525462765697993e-223, -1.1429360314275701698e-239,
  3.2397620015697148712e-192, 3.1030547578511949035e-208, -1.609965144193984205e-224, -1.8313007053436627876e-240,
  3.2397620015697148712e-192, 3.1030547578511949035e-208, -1.609965144193984205e-224, -1.8313007053436627876e-240,
  3.2397620015697148712e-192, 3.1030547578511949035e-208, -1.609965144193984205e-224, -1.8313007053436627876e-240,
  3.2397620015697148712e-192, 3.1030547578511949035e-208, -1.609965144193984205e-224, -1.8313007053436627876e-240,
  3.2397620015697148712e-192, 3.1030547578511949035e-208, -1.609965144193984205e-224, -1.8313007053436627876e-240,
  3.2397620015697148712e-192, 3.1030547578511949035e-208, -1.609965144193984205e-224, -1.8313007053436627876e-240,
  1.4863145223629928288e-192, -7.9038076992129241506e-209, -1.609965144193984205e-224, -1.8313007053436627876e-240,
  6.0959078275963141821e-193, 1.156336993964950812e-208, 2.7126166236326293347e-224, -1.8313007053436627876e-240,
  1.712289129579509076e-193, 1.8297811202182925249e-209, 1.1003018740995688645e-226, 5.827891678485165325e-243,
  1.712289129579509076e-193, 1.8297811202182925249e-209, 1.1003018740995688645e-226, 5.827891678485165325e-243,
  6.1638445507530779946e-194, -6.0361608463951204924e-210, 1.1003018740995688645e-226, 5.827891678485165325e-243,
  6.8432117823206978686e-195, 4.7332165749391048364e-212, 4.4984059688774601837e-228, -3.029900079464340522e-245,
  6.8432117823206978686e-195, 4.7332165749391048364e-212, 4.4984059688774601837e-228, -3.029900079464340522e-245,
  6.8432117823206978686e-195, 4.7332165749391048364e-212, 4.4984059688774601837e-228, -3.029900079464340522e-245,
  6.8432117823206978686e-195, 4.7332165749391048364e-212, 4.4984059688774601837e-228, -3.029900079464340522e-245,
  3.418509674495068119e-195, 4.7332165749391048364e-212, 4.4984059688774601837e-228, -3.029900079464340522e-245,
  1.7061586205822532442e-195, 4.7332165749391048364e-212, 4.4984059688774601837e-228, -3.029900079464340522e-245,
  8.499830936258458068e-196, 4.7332165749391048364e-212, 4.4984059688774601837e-228, -3.029900079464340522e-245,
  4.218953301476420881e-196, 4.7332165749391048364e-212, 4.4984059688774601837e-228, -3.029900079464340522e-245,
  2.0785144840854027628e-196, -1.9512340798794268979e-214, -3.6162764918921692779e-230, -2.8387319855193022476e-246,
  1.008295075389893466e-196, -1.9512340798794268979e-214, -3.6162764918921692779e-230, -2.8387319855193022476e-246,
  4.7318537104213881764e-197, -1.9512340798794268979e-214, -3.6162764918921692779e-230, -2.8387319855193022476e-246,
  2.0563051886826149345e-197, -1.9512340798794268979e-214, -3.6162764918921692779e-230, -2.8387319855193022476e-246,
  7.185309278132283136e-198, -1.9512340798794268979e-214, -3.6162764918921692779e-230, -2.8387319855193022476e-246,
  4.9643797378534984559e-199, -9.4699347169310243473e-216, -9.2331809177749077733e-233, -1.4042876247421728101e-248,
  4.9643797378534984559e-199, -9.4699347169310243473e-216, -9.2331809177749077733e-233, -1.4042876247421728101e-248,
  4.9643797378534984559e-199, -9.4699347169310243473e-216, -9.2331809177749077733e-233, -1.4042876247421728101e-248,
  4.9643797378534984559e-199, -9.4699347169310243473e-216, -9.2331809177749077733e-233, -1.4042876247421728101e-248,
  7.8383517263666503337e-200, 1.3736749441945438342e-215, -9.2331809177749077733e-233, -1.4042876247421728101e-248,
  7.8383517263666503337e-200, 1.3736749441945438342e-215, -9.2331809177749077733e-233, -1.4042876247421728101e-248,
  7.8383517263666503337e-200, 1.3736749441945438342e-215, -9.2331809177749077733e-233, -1.4042876247421728101e-248,
  2.612671019845610006e-200, 2.1334073625072069974e-216, -9.2331809177749077733e-233, -1.4042876247421728101e-248,
  2.612671019845610006e-200, 2.1334073625072069974e-216, -9.2331809177749077733e-233, -1.4042876247421728101e-248,
  1.306250843215349634e-200, 2.1334073625072069974e-216, -9.2331809177749077733e-233, -1.4042876247421728101e-248,
  6.5304075490021959302e-201, 6.8298960257742791824e-217, 6.8696910062179237095e-233, 3.8349029251851101018e-249,
  3.2643571074265457254e-201, -4.2219277387461470355e-218, -1.753154605289404553e-234, -7.5861268822635538093e-251,
  1.6313318866387202604e-201, -4.2219277387461470355e-218, -1.753154605289404553e-234, -7.5861268822635538093e-251,
  8.1481927624480752786e-202, -4.2219277387461470355e-218, -1.753154605289404553e-234, -7.5861268822635538093e-251,
  4.0656297104785107096e-202, 4.8431832608149701961e-218, 8.3111403472061145651e-234, 1.6001805286092554504e-249,
  2.0243481844937293316e-202, 3.1062776103441183191e-219, 7.6291913283447536617e-235, 2.0347903074934629333e-250,
  1.0037074215013384159e-202, 3.1062776103441183191e-219, 7.6291913283447536617e-235, 2.0347903074934629333e-250,
  4.9338704000514295811e-203, 3.1062776103441183191e-219, 7.6291913283447536617e-235, 2.0347903074934629333e-250,
  2.3822684925704522921e-203, 3.1062776103441183191e-219, 7.6291913283447536617e-235, 2.0347903074934629333e-250,
  1.1064675388299639308e-203, 2.7343042298126957741e-220, 5.5273393987134252385e-236, 1.1432574793608782288e-251,
  4.6856706195971960852e-204, 2.7343042298126957741e-220, 5.5273393987134252385e-236, 1.1432574793608782288e-251,
  1.4961682352459748279e-204, -8.0675475439086544798e-221, -3.6970842501441777651e-237, -5.7032870362481275794e-253,
  1.4961682352459748279e-204, -8.0675475439086544798e-221, -3.6970842501441777651e-237, -5.7032870362481275794e-253,
  6.9879263915816924805e-205, 9.6377473771091526132e-221, 1.5959741828948633012e-236, 2.7031904319843495713e-252,
  3.0010484111426663515e-205, 7.8509991660024955813e-222, 1.2171222696290252021e-237, -2.4742181023285720738e-254,
  1.0076094209231528444e-205, 7.8509991660024955813e-222, 1.2171222696290252021e-237, -2.4742181023285720738e-254,
  1.0889925813396166947e-207, 2.4325525462765697993e-223, -1.1429360314275701698e-239, 8.3218722366085688343e-256,
  1.0889925813396166947e-207, 2.4325525462765697993e-223, -1.1429360314275701698e-239, 8.3218722366085688343e-256,
  1.0889925813396166947e-207, 2.4325525462765697993e-223, -1.1429360314275701698e-239, 8.3218722366085688343e-256,
  1.0889925813396166947e-207, 2.4325525462765697993e-223, -1.1429360314275701698e-239, 8.3218722366085688343e-256,
  1.0889925813396166947e-207, 2.4325525462765697993e-223, -1.1429360314275701698e-239, 8.3218722366085688343e-256,
  1.0889925813396166947e-207, 2.4325525462765697993e-223, -1.1429360314275701698e-239, 8.3218722366085688343e-256,
  1.0889925813396166947e-207, 2.4325525462765697993e-223, -1.1429360314275701698e-239, 8.3218722366085688343e-256,
  3.1030547578511949035e-208, -1.609965144193984205e-224, -1.8313007053436625212e-240, -2.3341145329525059632e-256,
  3.1030547578511949035e-208, -1.609965144193984205e-224, -1.8313007053436625212e-240, -2.3341145329525059632e-256,
  1.156336993964950812e-208, 2.7126166236326293347e-224, -1.8313007053436625212e-240, -2.3341145329525059632e-256,
  1.8297811202182925249e-209, 1.1003018740995688645e-226, 5.827891678485165325e-243, -3.1174271110208206547e-259,
  1.8297811202182925249e-209, 1.1003018740995688645e-226, 5.827891678485165325e-243, -3.1174271110208206547e-259,
  1.8297811202182925249e-209, 1.1003018740995688645e-226, 5.827891678485165325e-243, -3.1174271110208206547e-259,
  6.1308251778939023781e-210, 1.1003018740995688645e-226, 5.827891678485165325e-243, -3.1174271110208206547e-259,
  4.7332165749391048364e-212, 4.4984059688774601837e-228, -3.0299000794643401155e-245, -2.8075477999879273582e-261,
  4.7332165749391048364e-212, 4.4984059688774601837e-228, -3.0299000794643401155e-245, -2.8075477999879273582e-261,
  4.7332165749391048364e-212, 4.4984059688774601837e-228, -3.0299000794643401155e-245, -2.8075477999879273582e-261,
  4.7332165749391048364e-212, 4.4984059688774601837e-228, -3.0299000794643401155e-245, -2.8075477999879273582e-261,
  4.7332165749391048364e-212, 4.4984059688774601837e-228, -3.0299000794643401155e-245, -2.8075477999879273582e-261,
  4.7332165749391048364e-212, 4.4984059688774601837e-228, -3.0299000794643401155e-245, -2.8075477999879273582e-261,
  4.7332165749391048364e-212, 4.4984059688774601837e-228, -3.0299000794643401155e-245, -2.8075477999879273582e-261,
  4.7332165749391048364e-212, 4.4984059688774601837e-228, -3.0299000794643401155e-245, -2.8075477999879273582e-261,
  2.3568521170701555846e-212, -7.7818310317651142243e-229, -3.0299000794643401155e-245, -2.8075477999879273582e-261,
  1.1686698881356804311e-212, 1.8601114328504743806e-228, -3.0299000794643401155e-245, -2.8075477999879273582e-261,
  5.7457877366844311816e-213, 5.409641648369814791e-229, -3.0299000794643401155e-245, -2.8075477999879273582e-261,
  2.7753321643482446169e-213, -1.1860946916976500828e-229, 6.3146909508553973881e-246, 1.2573885592501532045e-261,
  1.290104378180150675e-213, 2.1117734783360818049e-229, 4.2928382696354204061e-245, -2.8075477999879273582e-261,
  5.4749048509610403382e-214, 4.6283939331921604413e-230, 6.3146909508553973881e-246, 1.2573885592501532045e-261,
  1.7618353855408067201e-214, 5.060587206499956961e-231, 5.9380161562121075096e-247, -1.2904053011746964278e-263,
  1.7618353855408067201e-214, 5.060587206499956961e-231, 5.9380161562121075096e-247, -1.2904053011746964278e-263,
  8.3356801918574821257e-215, 5.060587206499956961e-231, 5.9380161562121075096e-247, -1.2904053011746964278e-263,
  3.6943433600821895879e-215, 5.060587206499956961e-231, 5.9380161562121075096e-247, -1.2904053011746964278e-263,
  1.3736749441945438342e-215, -9.2331809177749077733e-233, -1.4042876247421726117e-248, -9.9505977179164858712e-265,
  2.1334073625072069974e-216, -9.2331809177749077733e-233, -1.4042876247421726117e-248, -9.9505977179164858712e-265,
  2.1334073625072069974e-216, -9.2331809177749077733e-233, -1.4042876247421726117e-248, -9.9505977179164858712e-265,
  2.1334073625072069974e-216, -9.2331809177749077733e-233, -1.4042876247421726117e-248, -9.9505977179164858712e-265,
  6.8298960257742791824e-217, 6.8696910062179237095e-233, 3.8349029251851101018e-249, -2.6436684620390282645e-267,
  6.8298960257742791824e-217, 6.8696910062179237095e-233, 3.8349029251851101018e-249, -2.6436684620390282645e-267,
  3.2038516259498326923e-217, -1.1817449557784924788e-233, -6.3454186796659920093e-250, -2.6436684620390282645e-267,
  1.3908294260376086421e-217, 2.8439730252197153919e-233, 3.8349029251851101018e-249, -2.6436684620390282645e-267,
  4.8431832608149701961e-218, 8.3111403472061145651e-234, 1.6001805286092554504e-249, -2.6436684620390282645e-267,
  3.1062776103441183191e-219, 7.6291913283447536617e-235, 2.0347903074934629333e-250, -2.6436684620390282645e-267,
  3.1062776103441183191e-219, 7.6291913283447536617e-235, 2.0347903074934629333e-250, -2.6436684620390282645e-267,
  3.1062776103441183191e-219, 7.6291913283447536617e-235, 2.0347903074934629333e-250, -2.6436684620390282645e-267,
  3.1062776103441183191e-219, 7.6291913283447536617e-235, 2.0347903074934629333e-250, -2.6436684620390282645e-267,
  2.7343042298126957741e-220, 5.5273393987134252385e-236, 1.1432574793608780349e-251, 1.2329569415922591084e-267,
  2.7343042298126957741e-220, 5.5273393987134252385e-236, 1.1432574793608780349e-251, 1.2329569415922591084e-267,
  2.7343042298126957741e-220, 5.5273393987134252385e-236, 1.1432574793608780349e-251, 1.2329569415922591084e-267,
  2.7343042298126957741e-220, 5.5273393987134252385e-236, 1.1432574793608780349e-251, 1.2329569415922591084e-267,
  9.6377473771091526132e-221, 1.5959741828948633012e-236, 2.7031904319843490867e-252, 2.638005906844372114e-268,
  7.8509991660024955813e-222, 1.2171222696290252021e-237, -2.4742181023285720738e-254, -1.2030990169203137715e-270,
  7.8509991660024955813e-222, 1.2171222696290252021e-237, -2.4742181023285720738e-254, -1.2030990169203137715e-270,
  7.8509991660024955813e-222, 1.2171222696290252021e-237, -2.4742181023285720738e-254, -1.2030990169203137715e-270,
  7.8509991660024955813e-222, 1.2171222696290252021e-237, -2.4742181023285720738e-254, -1.2030990169203137715e-270,
  2.318094503184431479e-222, -1.1429360314275701698e-239, 8.3218722366085688343e-256, -2.0046830753539155726e-272,
  2.318094503184431479e-222, -1.1429360314275701698e-239, 8.3218722366085688343e-256, -2.0046830753539155726e-272,
  9.3486833747991514629e-223, -1.1429360314275701698e-239, 8.3218722366085688343e-256, -2.0046830753539155726e-272,
  2.4325525462765697993e-223, -1.1429360314275701698e-239, 8.3218722366085688343e-256, -2.0046830753539155726e-272,
  2.4325525462765697993e-223, -1.1429360314275701698e-239, 8.3218722366085688343e-256, -2.0046830753539155726e-272,
  7.0351983914592419146e-224, 7.766758903588374524e-240, 8.3218722366085688343e-256, -2.0046830753539155726e-272,
  7.0351983914592419146e-224, 7.766758903588374524e-240, 8.3218722366085688343e-256, -2.0046830753539155726e-272,
  2.7126166236326293347e-224, -1.8313007053436625212e-240, -2.3341145329525056675e-256, -2.0046830753539155726e-272,
  5.5132573971932232487e-225, 5.6821419688934674008e-241, 3.2988215943776273615e-257, 2.1353977370878701046e-273,
  5.5132573971932232487e-225, 5.6821419688934674008e-241, 3.2988215943776273615e-257, 2.1353977370878701046e-273,
  1.1003018740995688645e-226, 5.827891678485165325e-243, -3.117427111020820077e-259, -5.9718623963762788119e-275,
  1.1003018740995688645e-226, 5.827891678485165325e-243, -3.117427111020820077e-259, -5.9718623963762788119e-275,
  1.1003018740995688645e-226, 5.827891678485165325e-243, -3.117427111020820077e-259, -5.9718623963762788119e-275,
  1.1003018740995688645e-226, 5.827891678485165325e-243, -3.117427111020820077e-259, -5.9718623963762788119e-275,
  1.1003018740995688645e-226, 5.827891678485165325e-243, -3.117427111020820077e-259, -5.9718623963762788119e-275,
  1.1003018740995688645e-226, 5.827891678485165325e-243, -3.117427111020820077e-259, -5.9718623963762788119e-275,
  2.560476225709334075e-227, 5.827891678485165325e-243, -3.117427111020820077e-259, -5.9718623963762788119e-275,
  2.560476225709334075e-227, 5.827891678485165325e-243, -3.117427111020820077e-259, -5.9718623963762788119e-275,
  4.4984059688774601837e-228, -3.0299000794643401155e-245, -2.8075477999879273582e-261, -1.472095602234059958e-277,
  4.4984059688774601837e-228, -3.0299000794643401155e-245, -2.8075477999879273582e-261, -1.472095602234059958e-277,
  4.4984059688774601837e-228, -3.0299000794643401155e-245, -2.8075477999879273582e-261, -1.472095602234059958e-277,
  1.8601114328504743806e-228, -3.0299000794643401155e-245, -2.8075477999879273582e-261, -1.472095602234059958e-277,
  5.409641648369814791e-229, -3.0299000794643401155e-245, -2.8075477999879273582e-261, -1.472095602234059958e-277,
  5.409641648369814791e-229, -3.0299000794643401155e-245, -2.8075477999879273582e-261, -1.472095602234059958e-277,
  2.1117734783360818049e-229, 4.2928382696354204061e-245, -2.8075477999879273582e-261, -1.472095602234059958e-277,
  4.6283939331921604413e-230, 6.3146909508553973881e-246, 1.2573885592501529789e-261, 3.0408903374280139822e-277,
  4.6283939331921604413e-230, 6.3146909508553973881e-246, 1.2573885592501529789e-261, 3.0408903374280139822e-277,
  5.060587206499956961e-231, 5.9380161562121075096e-247, -1.2904053011746964278e-263, 8.7279092175580820317e-280,
  5.060587206499956961e-231, 5.9380161562121075096e-247, -1.2904053011746964278e-263, 8.7279092175580820317e-280,
  5.060587206499956961e-231, 5.9380161562121075096e-247, -1.2904053011746964278e-263, 8.7279092175580820317e-280,
  5.060587206499956961e-231, 5.9380161562121075096e-247, -1.2904053011746964278e-263, 8.7279092175580820317e-280,
  2.4841276986611042098e-231, 2.1712682097791944335e-248, 2.9746046415267896827e-264, -8.6516445844406224413e-282,
  1.1958979447416775482e-231, 2.1712682097791944335e-248, 2.9746046415267896827e-264, -8.6516445844406224413e-282,
  5.5178306778196421733e-232, 2.1712682097791944335e-248, 2.9746046415267896827e-264, -8.6516445844406224413e-282,
  2.2972562930210755192e-232, 2.1712682097791944335e-248, 2.9746046415267896827e-264, -8.6516445844406224413e-282,
  6.8696910062179237095e-233, 3.8349029251851101018e-249, -2.6436684620390282645e-267, -4.3807022524130141006e-284,
  6.8696910062179237095e-233, 3.8349029251851101018e-249, -2.6436684620390282645e-267, -4.3807022524130141006e-284,
  2.8439730252197153919e-233, 3.8349029251851101018e-249, -2.6436684620390282645e-267, -4.3807022524130141006e-284,
  8.3111403472061145651e-234, 1.6001805286092554504e-249, -2.6436684620390282645e-267, -4.3807022524130141006e-284,
  8.3111403472061145651e-234, 1.6001805286092554504e-249, -2.6436684620390282645e-267, -4.3807022524130141006e-284,
  3.2789928709583552854e-234, 4.8281933032132812475e-250, -2.6436684620390282645e-267, -4.3807022524130141006e-284,
  7.6291913283447536617e-235, 2.0347903074934629333e-250, -2.6436684620390282645e-267, -4.3807022524130141006e-284,
  7.6291913283447536617e-235, 2.0347903074934629333e-250, -2.6436684620390282645e-267, -4.3807022524130141006e-284,
  1.3390069830350552605e-235, -6.026193929640082176e-252, -7.0535576022338457803e-268, -4.3807022524130141006e-284,
  1.3390069830350552605e-235, -6.026193929640082176e-252, -7.0535576022338457803e-268, -4.3807022524130141006e-284,
  1.3390069830350552605e-235, -6.026193929640082176e-252, -7.0535576022338457803e-268, -4.3807022524130141006e-284,
  5.5273393987134252385e-236, 1.1432574793608780349e-251, 1.2329569415922591084e-267, -4.3807022524130141006e-284,
  1.5959741828948633012e-236, 2.7031904319843490867e-252, 2.638005906844371576e-268, 6.3790946999826013345e-284,
  1.5959741828948633012e-236, 2.7031904319843490867e-252, 2.638005906844371576e-268, 6.3790946999826013345e-284,
  6.1313287894022281692e-237, 5.2084434157824127104e-253, 2.1511502957481757317e-269, 3.2670891426006739096e-285,
  1.2171222696290252021e-237, -2.4742181023285720738e-254, -1.2030990169203137715e-270, -9.5347405022956042207e-287,
  1.2171222696290252021e-237, -2.4742181023285720738e-254, -1.2030990169203137715e-270, -9.5347405022956042207e-287,
  1.2171222696290252021e-237, -2.4742181023285720738e-254, -1.2030990169203137715e-270, -9.5347405022956042207e-287,
  6.0284645465737476297e-238, -2.4742181023285720738e-254, -1.2030990169203137715e-270, -9.5347405022956042207e-287,
  2.9570854717154947523e-238, 4.3456134301905148502e-254, 6.3684349745470443788e-270, -9.5347405022956042207e-287,
  1.4213959342863689955e-238, 9.3569766393097138822e-255, 2.5826679788133653036e-270, -9.5347405022956042207e-287,
  6.5355116557180594664e-239, 9.3569766393097138822e-255, 2.5826679788133653036e-270, -9.5347405022956042207e-287,
  2.6962878121452450746e-239, 8.3218722366085688343e-256, -2.0046830753539152442e-272, -3.4057806738724185961e-288,
  7.766758903588374524e-240, 8.3218722366085688343e-256, -2.0046830753539152442e-272, -3.4057806738724185961e-288,
  7.766758903588374524e-240, 8.3218722366085688343e-256, -2.0046830753539152442e-272, -3.4057806738724185961e-288,
  2.9677290991223565342e-240, -2.3341145329525056675e-256, -2.0046830753539152442e-272, -3.4057806738724185961e-288,
  5.6821419688934674008e-241, 3.2988215943776273615e-257, 2.1353977370878701046e-273, -1.2215123283371736879e-289,
  5.6821419688934674008e-241, 3.2988215943776273615e-257, 2.1353977370878701046e-273, -1.2215123283371736879e-289,
  5.6821419688934674008e-241, 3.2988215943776273615e-257, 2.1353977370878701046e-273, -1.2215123283371736879e-289,
  2.6827483411022054912e-241, 3.2988215943776273615e-257, 2.1353977370878701046e-273, -1.2215123283371736879e-289,
  1.1830515272065748694e-241, -3.117427111020820077e-259, -5.9718623963762788119e-275, 6.1155422068568954053e-291,
  4.3320312025875939195e-242, -3.117427111020820077e-259, -5.9718623963762788119e-275, 6.1155422068568954053e-291,
  5.827891678485165325e-243, -3.117427111020820077e-259, -5.9718623963762788119e-275, 6.1155422068568954053e-291,
  5.827891678485165325e-243, -3.117427111020820077e-259, -5.9718623963762788119e-275, 6.1155422068568954053e-291,
  5.827891678485165325e-243, -3.117427111020820077e-259, -5.9718623963762788119e-275, 6.1155422068568954053e-291,
  1.1413391350613183311e-243, -5.1586784110844895013e-260, -1.9524039360882352712e-276, -2.9779654517181717279e-292,
  1.1413391350613183311e-243, -5.1586784110844895013e-260, -1.9524039360882352712e-276, -2.9779654517181717279e-292,
  1.1413391350613183311e-243, -5.1586784110844895013e-260, -1.9524039360882352712e-276, -2.9779654517181717279e-292,
  5.5552006713333735927e-244, 7.8491179384773690214e-260, -1.9524039360882352712e-276, -2.9779654517181717279e-292,
  2.6261053316934700345e-244, 1.345219763696439399e-260, 1.6579848156414234801e-276, 1.0303712682997740506e-292,
  1.1615576618735179302e-244, 1.345219763696439399e-260, 1.6579848156414234801e-276, 1.0303712682997740506e-292,
  4.2928382696354204061e-245, -2.8075477999879273582e-261, -1.472095602234059958e-277, 2.8287088295287585094e-294,
  6.3146909508553973881e-246, 1.2573885592501529789e-261, 3.0408903374280139822e-277, 2.8287088295287585094e-294,
  6.3146909508553973881e-246, 1.2573885592501529789e-261, 3.0408903374280139822e-277, 2.8287088295287585094e-294,
  6.3146909508553973881e-246, 1.2573885592501529789e-261, 3.0408903374280139822e-277, 2.8287088295287585094e-294,
  1.7379794826680480784e-246, 2.4115446944063306384e-262, 2.202741251392177696e-278, 2.8287088295287585094e-294,
  1.7379794826680480784e-246, 2.4115446944063306384e-262, 2.202741251392177696e-278, 2.8287088295287585094e-294,
  5.9380161562121075096e-247, -1.2904053011746964278e-263, 8.7279092175580810531e-280, 8.8634899828990930877e-296,
  2.1712682097791944335e-248, 2.9746046415267896827e-264, -8.6516445844406224413e-282, -5.0528699238150276549e-299,
  2.1712682097791944335e-248, 2.9746046415267896827e-264, -8.6516445844406224413e-282, -5.0528699238150276549e-299,
  2.1712682097791944335e-248, 2.9746046415267896827e-264, -8.6516445844406224413e-282, -5.0528699238150276549e-299,
  2.1712682097791944335e-248, 2.9746046415267896827e-264, -8.6516445844406224413e-282, -5.0528699238150276549e-299,
  2.1712682097791944335e-248, 2.9746046415267896827e-264, -8.6516445844406224413e-282, -5.0528699238150276549e-299,
  3.8349029251851101018e-249, -2.6436684620390282645e-267, -4.3807022524130141006e-284, -2.7456019707854725967e-300,
  3.8349029251851101018e-249, -2.6436684620390282645e-267, -4.3807022524130141006e-284, -2.7456019707854725967e-300,
  3.8349029251851101018e-249, -2.6436684620390282645e-267, -4.3807022524130141006e-284, -2.7456019707854725967e-300,
  1.6001805286092554504e-249, -2.6436684620390282645e-267, -4.3807022524130141006e-284, -2.7456019707854725967e-300,
  4.8281933032132812475e-250, -2.6436684620390282645e-267, -4.3807022524130141006e-284, -2.7456019707854725967e-300,
  4.8281933032132812475e-250, -2.6436684620390282645e-267, -4.3807022524130141006e-284, -2.7456019707854725967e-300,
  2.0347903074934629333e-250, -2.6436684620390282645e-267, -4.3807022524130141006e-284, -2.7456019707854725967e-300,
  6.3808880963355377617e-251, -2.6436684620390282645e-267, -4.3807022524130141006e-284, -2.7456019707854725967e-300,
  6.3808880963355377617e-251, -2.6436684620390282645e-267, -4.3807022524130141006e-284, -2.7456019707854725967e-300,
  2.8891343516857640937e-251, 5.1095823452235464813e-267, -4.3807022524130141006e-284, -2.7456019707854725967e-300,
  1.1432574793608780349e-251, 1.2329569415922591084e-267, -4.3807022524130141006e-284, -2.7456019707854725967e-300,
  2.7031904319843490867e-252, 2.638005906844371576e-268, 6.3790946999826013345e-284, -2.7456019707854725967e-300,
  2.7031904319843490867e-252, 2.638005906844371576e-268, 6.3790946999826013345e-284, -2.7456019707854725967e-300,
  5.2084434157824127104e-253, 2.1511502957481757317e-269, 3.2670891426006735363e-285, 2.4084160842482777461e-301,
  5.2084434157824127104e-253, 2.1511502957481757317e-269, 3.2670891426006735363e-285, 2.4084160842482777461e-301,
  5.2084434157824127104e-253, 2.1511502957481757317e-269, 3.2670891426006735363e-285, 2.4084160842482777461e-301,
  2.4805108027747776379e-253, 2.1511502957481757317e-269, 3.2670891426006735363e-285, 2.4084160842482777461e-301,
  1.1165444962709601017e-253, 2.1511502957481757317e-269, 3.2670891426006735363e-285, 2.4084160842482777461e-301,
  4.3456134301905148502e-254, 6.3684349745470443788e-270, -9.5347405022956030541e-287, -1.5805886663557401565e-302,
  9.3569766393097138822e-255, 2.5826679788133653036e-270, -9.5347405022956030541e-287, -1.5805886663557401565e-302,
  9.3569766393097138822e-255, 2.5826679788133653036e-270, -9.5347405022956030541e-287, -1.5805886663557401565e-302,
  8.3218722366085688343e-256, -2.0046830753539152442e-272, -3.4057806738724185961e-288, 2.3458177946667328156e-304,
  8.3218722366085688343e-256, -2.0046830753539152442e-272, -3.4057806738724185961e-288, 2.3458177946667328156e-304,
  8.3218722366085688343e-256, -2.0046830753539152442e-272, -3.4057806738724185961e-288, 2.3458177946667328156e-304,
  8.3218722366085688343e-256, -2.0046830753539152442e-272, -3.4057806738724185961e-288, 2.3458177946667328156e-304,
  2.9938788518280315834e-256, -2.0046830753539152442e-272, -3.4057806738724185961e-288, 2.3458177946667328156e-304,
  3.2988215943776273615e-257, 2.1353977370878701046e-273, -1.2215123283371736879e-289, 6.7342163555358599277e-306,
  3.2988215943776273615e-257, 2.1353977370878701046e-273, -1.2215123283371736879e-289, 6.7342163555358599277e-306,
  3.2988215943776273615e-257, 2.1353977370878701046e-273, -1.2215123283371736879e-289, 6.7342163555358599277e-306,
  3.2988215943776273615e-257, 2.1353977370878701046e-273, -1.2215123283371736879e-289, 6.7342163555358599277e-306,
  1.6338236616337094706e-257, 2.1353977370878701046e-273, -1.2215123283371736879e-289, 6.7342163555358599277e-306,
  8.0132469526175071002e-258, 2.8687869620228451614e-274, -1.9537812801257956865e-290, 1.0380272777574237546e-306,
  3.850752120757712373e-258, 2.8687869620228451614e-274, -1.9537812801257956865e-290, 1.0380272777574237546e-306,
  1.7695047048278150093e-258, 2.8687869620228451614e-274, -1.9537812801257956865e-290, 1.0380272777574237546e-306,
  7.2888099686286655858e-259, 5.581381609158630475e-275, 6.1155422068568946933e-291, 1.0380272777574237546e-306,
  2.0856914288039227544e-259, -1.9524039360882352712e-276, -2.9779654517181712829e-292, -3.000817432603284506e-308,
  2.0856914288039227544e-259, -1.9524039360882352712e-276, -2.9779654517181712829e-292, -3.000817432603284506e-308,
  7.8491179384773690214e-260, -1.9524039360882352712e-276, -2.9779654517181712829e-292, -3.000817432603284506e-308,
  1.345219763696439399e-260, 1.6579848156414234801e-276, 1.0303712682997738281e-292, 1.4493302844111182601e-308,
  1.345219763696439399e-260, 1.6579848156414234801e-276, 1.0303712682997738281e-292, 1.4493302844111182601e-308,
  1.345219763696439399e-260, 1.6579848156414234801e-276, 1.0303712682997738281e-292, 1.4493302844111182601e-308,
  5.3223249184882342185e-261, -1.472095602234059958e-277, 2.8287088295287585094e-294, -1.0874435234232647519e-310,
  1.2573885592501529789e-261, 3.0408903374280139822e-277, 2.8287088295287585094e-294, -1.0874435234232647519e-310,
  1.2573885592501529789e-261, 3.0408903374280139822e-277, 2.8287088295287585094e-294, -1.0874435234232647519e-310,
  2.4115446944063306384e-262, 2.202741251392177696e-278, 2.8287088295287585094e-294, -1.0874435234232647519e-310,
  2.4115446944063306384e-262, 2.202741251392177696e-278, 2.8287088295287585094e-294, -1.0874435234232647519e-310,
  2.4115446944063306384e-262, 2.202741251392177696e-278, 2.8287088295287585094e-294, -1.0874435234232647519e-310,
  1.1412520821444306741e-262, -6.1787496089661820348e-279, -3.028042329852615431e-295, -2.182740474438892116e-311,
  5.0610577601348040988e-263, 7.9243314524777990283e-279, -3.028042329852615431e-295, -2.182740474438892116e-311,
  1.8853262294800541881e-263, 8.7279092175580810531e-280, 8.8634899828990930877e-296, -9.8167844904532653004e-314,
  2.9746046415267896827e-264, -8.6516445844406224413e-282, -5.0528699238150265939e-299, -1.3288013265921760399e-314,
  2.9746046415267896827e-264, -8.6516445844406224413e-282, -5.0528699238150265939e-299, -1.3288013265921760399e-314,
  2.9746046415267896827e-264, -8.6516445844406224413e-282, -5.0528699238150265939e-299, -1.3288013265921760399e-314,
  9.8977243486757054781e-265, -8.6516445844406224413e-282, -5.0528699238150265939e-299, -1.3288013265921760399e-314,
  9.8977243486757054781e-265, -8.6516445844406224413e-282, -5.0528699238150265939e-299, -1.3288013265921760399e-314,
  4.9356438320276576408e-265, -8.6516445844406224413e-282, -5.0528699238150265939e-299, -1.3288013265921760399e-314,
  2.4546035737036337221e-265, -8.6516445844406224413e-282, -5.0528699238150265939e-299, -1.3288013265921760399e-314,
  1.2140834445416214873e-265, 1.8893435613692150014e-281, 3.0075895258731974416e-297, -9.8167844904532653004e-314,
  5.9382337996061564537e-266, 5.1208955146257653156e-282, -5.0528699238150265939e-299, -1.3288013265921760399e-314,
  2.8369334767011265554e-266, 5.1208955146257653156e-282, -5.0528699238150265939e-299, -1.3288013265921760399e-314,
  1.2862833152486119506e-266, 1.6777604898591683764e-282, -5.0528699238150265939e-299, -1.3288013265921760399e-314,
  5.1095823452235464813e-267, -4.3807022524130141006e-284, -2.7456019707854725967e-300, -2.5539572388808429997e-317,
  1.2329569415922591084e-267, -4.3807022524130141006e-284, -2.7456019707854725967e-300, -2.5539572388808429997e-317,
  1.2329569415922591084e-267, -4.3807022524130141006e-284, -2.7456019707854725967e-300, -2.5539572388808429997e-317,
  2.638005906844371576e-268, 6.3790946999826013345e-284, -2.7456019707854725967e-300, -2.5539572388808429997e-317,
  2.638005906844371576e-268, 6.3790946999826013345e-284, -2.7456019707854725967e-300, -2.5539572388808429997e-317,
  2.1511502957481757317e-269, 3.2670891426006735363e-285, 2.4084160842482773317e-301, 5.7350888195772519812e-317,
  2.1511502957481757317e-269, 3.2670891426006735363e-285, 2.4084160842482773317e-301, 5.7350888195772519812e-317,
  2.1511502957481757317e-269, 3.2670891426006735363e-285, 2.4084160842482773317e-301, 5.7350888195772519812e-317,
  2.1511502957481757317e-269, 3.2670891426006735363e-285, 2.4084160842482773317e-301, 5.7350888195772519812e-317,
  6.3684349745470443788e-270, -9.5347405022956030541e-287, -1.5805886663557401565e-302, 3.6369654387311681856e-319,
  6.3684349745470443788e-270, -9.5347405022956030541e-287, -1.5805886663557401565e-302, 3.6369654387311681856e-319,
  2.5826679788133653036e-270, -9.5347405022956030541e-287, -1.5805886663557401565e-302, 3.6369654387311681856e-319,
  6.8978448094652555593e-271, 1.1480487920352081009e-286, 7.5257037990230704094e-303, 3.6369654387311681856e-319,
  6.8978448094652555593e-271, 1.1480487920352081009e-286, 7.5257037990230704094e-303, 3.6369654387311681856e-319,
  2.1656360647981577662e-271, 9.7287370902823839435e-288, 1.6928061833779524157e-303, 3.6369654387311681856e-319,
  2.1656360647981577662e-271, 9.7287370902823839435e-288, 1.6928061833779524157e-303, 3.6369654387311681856e-319,
  9.825838786313830552e-272, 9.7287370902823839435e-288, 1.6928061833779524157e-303, 3.6369654387311681856e-319,
  3.9105778554799569972e-272, 9.7287370902823839435e-288, 1.6928061833779524157e-303, 3.6369654387311681856e-319,
  9.5294739006302120482e-273, -1.2215123283371736879e-289, 6.7342163555358599277e-306, -5.681754927174335258e-322,
  9.5294739006302120482e-273, -1.2215123283371736879e-289, 6.7342163555358599277e-306, -5.681754927174335258e-322,
  2.1353977370878701046e-273, -1.2215123283371736879e-289, 6.7342163555358599277e-306, -5.681754927174335258e-322,
  2.1353977370878701046e-273, -1.2215123283371736879e-289, 6.7342163555358599277e-306, -5.681754927174335258e-322,
  2.8687869620228451614e-274, -1.9537812801257956865e-290, 1.0380272777574237546e-306, 6.4228533959362050743e-323,
}};
 __attribute__((aligned(64)))
const float PayneHanekReductionTable_float[] = {{
    // clang-format off
  0.159154892, 5.112411827e-08, 3.626141271e-15, -2.036222915e-22,
  0.03415493667, 6.420638243e-09, 7.342738037e-17, 8.135951656e-24,
  0.03415493667, 6.420638243e-09, 7.342738037e-17, 8.135951656e-24,
  0.002904943191, -9.861969574e-11, -9.839336547e-18, -1.790215892e-24,
  0.002904943191, -9.861969574e-11, -9.839336547e-18, -1.790215892e-24,
  0.002904943191, -9.861969574e-11, -9.839336547e-18, -1.790215892e-24,
  0.002904943191, -9.861969574e-11, -9.839336547e-18, -1.790215892e-24,
  0.0009518179577, 1.342109202e-10, 1.791623576e-17, 1.518506657e-24,
  0.0009518179577, 1.342109202e-10, 1.791623576e-17, 1.518506657e-24,
  0.0004635368241, 1.779561221e-11, 4.038449606e-18, -1.358546052e-25,
  0.0002193961991, 1.779561221e-11, 4.038449606e-18, -1.358546052e-25,
  9.73258866e-05, 1.779561221e-11, 4.038449606e-18, -1.358546052e-25,
  3.62907449e-05, 3.243700447e-12, 5.690024473e-19, 7.09405479e-26,
  5.773168596e-06, 1.424711477e-12, 1.3532163e-19, 1.92417627e-26,
  5.773168596e-06, 1.424711477e-12, 1.3532163e-19, 1.92417627e-26,
  5.773168596e-06, 1.424711477e-12, 1.3532163e-19, 1.92417627e-26,
  1.958472239e-06, 5.152167755e-13, 1.3532163e-19, 1.92417627e-26,
  5.112411827e-08, 3.626141271e-15, -2.036222915e-22, 6.177847236e-30,
  5.112411827e-08, 3.626141271e-15, -2.036222915e-22, 6.177847236e-30,
  5.112411827e-08, 3.626141271e-15, -2.036222915e-22, 6.177847236e-30,
  5.112411827e-08, 3.626141271e-15, -2.036222915e-22, 6.177847236e-30,
  5.112411827e-08, 3.626141271e-15, -2.036222915e-22, 6.177847236e-30,
  5.112411827e-08, 3.626141271e-15, -2.036222915e-22, 6.177847236e-30,
  2.132179588e-08, 3.626141271e-15, -2.036222915e-22, 6.177847236e-30,
  6.420638243e-09, 7.342738037e-17, 8.135951656e-24, -1.330400526e-31,
  6.420638243e-09, 7.342738037e-17, 8.135951656e-24, -1.330400526e-31,
  2.695347945e-09, 7.342738037e-17, 8.135951656e-24, -1.330400526e-31,
  8.327027956e-10, 7.342738037e-17, 8.135951656e-24, -1.330400526e-31,
  8.327027956e-10, 7.342738037e-17, 8.135951656e-24, -1.330400526e-31,
  3.670415083e-10, 7.342738037e-17, 8.135951656e-24, -1.330400526e-31,
  1.342109202e-10, 1.791623576e-17, 1.518506361e-24, 2.613904e-31,
  1.779561221e-11, 4.038449606e-18, -1.358545683e-25, -3.443243946e-32,
  1.779561221e-11, 4.038449606e-18, -1.358545683e-25, -3.443243946e-32,
  1.779561221e-11, 4.038449606e-18, -1.358545683e-25, -3.443243946e-32,
  3.243700447e-12, 5.690024473e-19, 7.094053557e-26, 1.487136711e-32,
  3.243700447e-12, 5.690024473e-19, 7.094053557e-26, 1.487136711e-32,
  3.243700447e-12, 5.690024473e-19, 7.094053557e-26, 1.487136711e-32,
  1.424711477e-12, 1.3532163e-19, 1.924175961e-26, 2.545416018e-33,
  5.152167755e-13, 1.3532163e-19, 1.924175961e-26, 2.545416018e-33,
  6.046956013e-14, -2.036222915e-22, 6.177846108e-30, 1.082084378e-36,
  6.046956013e-14, -2.036222915e-22, 6.177846108e-30, 1.082084378e-36,
  6.046956013e-14, -2.036222915e-22, 6.177846108e-30, 1.082084378e-36,
  3.626141271e-15, -2.036222915e-22, 6.177846108e-30, 1.082084378e-36,
  3.626141271e-15, -2.036222915e-22, 6.177846108e-30, 1.082084378e-36,
  3.626141271e-15, -2.036222915e-22, 6.177846108e-30, 1.082084378e-36,
  3.626141271e-15, -2.036222915e-22, 6.177846108e-30, 1.082084378e-36,
  7.342738037e-17, 8.135951656e-24, -1.330400526e-31, 6.296048013e-40,
  7.342738037e-17, 8.135951656e-24, -1.330400526e-31, 6.296048013e-40,
  7.342738037e-17, 8.135951656e-24, -1.330400526e-31, 6.296048013e-40,
  7.342738037e-17, 8.135951656e-24, -1.330400526e-31, 6.296048013e-40,
  7.342738037e-17, 8.135951656e-24, -1.330400526e-31, 6.296048013e-40,
  7.342738037e-17, 8.135951656e-24, -1.330400526e-31, 6.296048013e-40,
  1.791623576e-17, 1.518506361e-24, 2.61390353e-31, 4.764937743e-38,
  1.791623576e-17, 1.518506361e-24, 2.61390353e-31, 4.764937743e-38,
  4.038449606e-18, -1.358545683e-25, -3.443243946e-32, 6.296048013e-40,
  4.038449606e-18, -1.358545683e-25, -3.443243946e-32, 6.296048013e-40,
  5.690024473e-19, 7.094053557e-26, 1.487136711e-32, 6.296048013e-40,
  5.690024473e-19, 7.094053557e-26, 1.487136711e-32, 6.296048013e-40,
  5.690024473e-19, 7.094053557e-26, 1.487136711e-32, 6.296048013e-40,
  1.3532163e-19, 1.924175961e-26, 2.545415467e-33, 6.296048013e-40,
  1.3532163e-19, 1.924175961e-26, 2.545415467e-33, 6.296048013e-40,
  2.690143217e-20, -1.452834402e-28, -6.441077673e-36, -1.764234767e-42,
  2.690143217e-20, -1.452834402e-28, -6.441077673e-36, -1.764234767e-42,
  2.690143217e-20, -1.452834402e-28, -6.441077673e-36, -1.764234767e-42,
  1.334890502e-20, -1.452834402e-28, -6.441077673e-36, -1.764234767e-42,
  6.572641438e-21, -1.452834402e-28, -6.441077673e-36, -1.764234767e-42,
  0.05874381959, 1.222115387e-08, 7.693612965e-16, 1.792054435e-22,
  0.02749382704, 4.77057327e-09, 7.693612965e-16, 1.792054435e-22,
  0.01186883077, 1.045283415e-09, 3.252721926e-16, 7.332633139e-23,
  0.00405633077, 1.045283415e-09, 3.252721926e-16, 7.332633139e-23,
  0.000150081818, -2.454155802e-12, 1.161414894e-20, 1.291319272e-27,
  0.000150081818, -2.454155802e-12, 1.161414894e-20, 1.291319272e-27,
  0.000150081818, -2.454155802e-12, 1.161414894e-20, 1.291319272e-27,
  0.000150081818, -2.454155802e-12, 1.161414894e-20, 1.291319272e-27,
  0.000150081818, -2.454155802e-12, 1.161414894e-20, 1.291319272e-27,
  2.801149822e-05, 4.821800945e-12, 8.789757674e-19, 1.208447639e-25,
  2.801149822e-05, 4.821800945e-12, 8.789757674e-19, 1.208447639e-25,
  2.801149822e-05, 4.821800945e-12, 8.789757674e-19, 1.208447639e-25,
  1.275271279e-05, 1.183823005e-12, 1.161414894e-20, 1.291319272e-27,
  5.12331826e-06, 1.183823005e-12, 1.161414894e-20, 1.291319272e-27,
  1.308621904e-06, 2.743283031e-13, 1.161414894e-20, 1.291319272e-27,
  1.308621904e-06, 2.743283031e-13, 1.161414894e-20, 1.291319272e-27,
  3.549478151e-07, 4.695462769e-14, 1.161414894e-20, 1.291319272e-27,
  3.549478151e-07, 4.695462769e-14, 1.161414894e-20, 1.291319272e-27,
  1.165292645e-07, 1.853292503e-14, 4.837885366e-21, 1.291319272e-27,
  1.165292645e-07, 1.853292503e-14, 4.837885366e-21, 1.291319272e-27,
  5.69246339e-08, 4.322073705e-15, 1.449754789e-21, 7.962890365e-29,
  2.712231151e-08, 4.322073705e-15, 1.449754789e-21, 7.962890365e-29,
  1.222115387e-08, 7.693612965e-16, 1.792054182e-22, 2.91418027e-29,
  4.77057327e-09, 7.693612965e-16, 1.792054182e-22, 2.91418027e-29,
  1.045283415e-09, 3.252721926e-16, 7.332632508e-23, 3.898253736e-30,
  1.045283415e-09, 3.252721926e-16, 7.332632508e-23, 3.898253736e-30,
  1.139611461e-10, 1.996093359e-17, 5.344349223e-25, 1.511644828e-31,
  1.139611461e-10, 1.996093359e-17, 5.344349223e-25, 1.511644828e-31,
  1.139611461e-10, 1.996093359e-17, 5.344349223e-25, 1.511644828e-31,
  1.139611461e-10, 1.996093359e-17, 5.344349223e-25, 1.511644828e-31,
  5.575349904e-11, 6.083145782e-18, 5.344349223e-25, 1.511644828e-31,
  2.664967552e-11, -8.557475018e-19, -8.595036458e-26, -2.139883875e-32,
  1.209775682e-11, 2.61369883e-18, 5.344349223e-25, 1.511644828e-31,
  4.821800945e-12, 8.789757674e-19, 1.208447639e-25, 3.253064536e-33,
  1.183823005e-12, 1.161414894e-20, 1.29131908e-27, 1.715766248e-34,
  1.183823005e-12, 1.161414894e-20, 1.29131908e-27, 1.715766248e-34,
  2.743283031e-13, 1.161414894e-20, 1.29131908e-27, 1.715766248e-34,
    // clang-format on
}};
#endif // HWY_ONCE
""")


if __name__ == "__main__":
    main()