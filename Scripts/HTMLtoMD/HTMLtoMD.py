import demistomock as demisto
from CommonServerPython import *
from markdownify import markdownify as md


def html_to_md_command(args):
    html = str(args.get('html'))
    markdown = md(html)
    result = {
        "Original": str(html),
        "Result": str(markdown)
    }
    outputs = {
        "HTMLtoMD(val.Original == obj.Original)": result
    }
    return result, markdown, outputs


def main():
    try:
        args = demisto.args()
        result, markdown, outputs = html_to_md_command(args)
        demisto.results({
            'Type': entryTypes['note'],
            'ContentsFormat': formats['markdown'],
            'Contents': result,
            'HumanReadable': markdown,
            'EntryContext': outputs
        })
    except Exception as expt:
        return_error(f'Failed to execute HTMLtoMD script. Error: {str(expt)}')


if __name__ in ('__main__', '__builtin__', 'builtins'):
    main()
