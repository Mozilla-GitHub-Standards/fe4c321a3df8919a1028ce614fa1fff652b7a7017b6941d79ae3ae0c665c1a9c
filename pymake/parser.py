"""
Module for parsing Makefile syntax.

Makefiles use a line-based parsing system. Continuations and substitutions are handled differently based on the
type of line being parsed:

Lines with makefile syntax condense continuations to a single space, no matter the actual trailing whitespace
of the first line or the leading whitespace of the continuation. In other situations, trailing whitespace is
relevant.

Lines with command syntax do not condense continuations: the backslash and newline are part of the command.
(GNU Make is buggy in this regard, at least on mac).

Lines with an initial tab are commands if they can be (there is a rule or a command immediately preceding).
Otherwise, they are parsed as makefile syntax.

After splitting data into parseable chunks, we use a recursive-descent parser to
nest parenthesized syntax.

This file parses into the data structures defined in the parserdata module. Those classes are what actually
do the dirty work of "executing" the parsed data into a Makefile data structure.
"""

import logging, re, os
import data, functions, util, parserdata

log = logging.getLogger('pymake.parser')

class SyntaxError(util.MakeError):
    pass

def findlast(func, iterable):
    for i in iterable:
        if func(i):
            f = i
        else:
            return f

    return f

class Data(object):
    """
    A single virtual "line", which can be multiple source lines joined with
    continuations.
    """

    def __init__(self):
        self.data = ""

        # _locs is a list of tuples
        # (dataoffset, location)
        self._locs = []

    @staticmethod
    def fromstring(str, loc):
        d = Data()
        d.append(str, loc)
        return d

    # def __len__(self):
    #     return len(self.data)

    # def __getitem__(self, key):
    #     try:
    #         return self.data[key]
    #     except IndexError:
    #         return None

    def append(self, data, loc):
        self._locs.append( (len(self.data), loc) )
        self.data += data

    def getloc(self, offset):
        """
        Get the location of an offset within data.
        """
        if offset is None or offset >= len(self.data):
            offset = len(self.data) - 1

        if offset == -1:
            offset = 0

        begin, loc = findlast(lambda (o, l): o <= offset, self._locs)
        return loc + self.data[begin:offset]

    def skipwhitespace(self, offset):
        """
        Return the offset into data after skipping whitespace.
        """
        while offset < len(self.data):
            c = self.data[offset]
            if not c.isspace():
                break
            offset += 1
        return offset

    def findtoken(self, o, tlist, skipws):
        """
        Check data at position o for any of the tokens in tlist followed by whitespace
        or end-of-data.

        If a token is found, skip trailing whitespace and return (token, newoffset).
        Otherwise return None, oldoffset
        """
        assert isinstance(tlist, TokenList)

        if skipws:
            m = tlist.wslist.match(self.data, pos=o)
            if m is not None:
                return m.group(1), m.end(0)
        else:
            m = tlist.simplere.match(self.data, pos=o)
            if m is not None:
                return m.group(0), m.end(0)

        return None, o

class DynamicData(Data):
    """
    If we're reading from a stream, allows reading additional data dynamically.
    """
    def __init__(self, lineiter, path):
        Data.__init__(self)
        self.lineiter = lineiter
        self.path = path

    def readline(self):
        try:
            lineno, line = self.lineiter.next()
            self.append(line, parserdata.Location(self.path, lineno, 0))
            return True
        except StopIteration:
            return False

makefiletokensescaped = [r'\\\\#', r'\\#', '\\\\\n', '\\\\\\s+\\\\\n', r'\\.', '#', '\n']
continuationtokensescaped = ['\\\\\n', r'\\.', '\n']

class TokenList(object):
    """
    A list of tokens to search. Because these lists are static, we can perform
    optimizations (such as escaping and compiling regexes) on construction.
    """
    def __init__(self, tlist):
        self.tlist = tlist
        self.emptylist = len(tlist) == 0
        escapedlist = [re.escape(t) for t in tlist]
        self.simplere = re.compile('|'.join(escapedlist))
        self.makefilere = re.compile('|'.join(escapedlist + makefiletokensescaped))
        self.continuationre = re.compile('|'.join(escapedlist + continuationtokensescaped))

        self.wslist = re.compile('(' + '|'.join(escapedlist) + ')' + r'(\s+|$)')

    imap = {}

    @staticmethod
    def get(s):
        if s in TokenList.imap:
            return TokenList.imap[s]

        i = TokenList(s)
        TokenList.imap[s] = i
        return i

emptytokenlist = TokenList.get('')

# The following four iterators handle line continuations and comments in
# different ways, but share a similar behavior:
#
# Called with (data, startoffset, tokenlist)
#
# yield 4-tuples (flatstr, token, tokenoffset, afteroffset)
# flatstr is data, guaranteed to have no tokens (may be '')
# token, tokenoffset, afteroffset *may be None*. That means there is more text
# coming.

def iterdata(d, offset, tokenlist):
    if tokenlist.emptylist:
        yield d.data, None, None, None
        return

    s = tokenlist.simplere

    while offset < len(d.data):
        m = s.search(d.data, pos=offset)
        if m is None:
            yield d.data[offset:], None, None, None
            return

        yield d.data[offset:m.start(0)], m.group(0), m.start(0), m.end(0)
        offset = m.end(0)

def itermakefilechars(d, offset, tokenlist):
    s = tokenlist.makefilere

    while offset < len(d.data):
        m = s.search(d.data, pos=offset)
        if m is None:
            yield d.data[offset:], None, None, None
            return

        token = m.group(0)
        start = m.start(0)
        end = m.end(0)

        if token == '\n':
            assert end == len(d.data)
            yield d.data[offset:start], None, None, None
            return

        if token == '#':
            yield d.data[offset:start], None, None, None
            for s in itermakefilechars(d, end, emptytokenlist): pass
            return

        if token == '\\\\#':
            # see escape-chars.mk VARAWFUL
            yield d.data[offset:start + 1], None, None, None
            for s in itermakefilechars(d, end, emptytokenlist): pass
            return

        if token == '\\\n':
            yield d.data[offset:start].rstrip() + ' ', None, None, None
            d.readline()
            offset = d.skipwhitespace(end)
            continue

        if token.startswith('\\') and token.endswith('\n'):
            assert end == len(d.data)
            yield d.data[offset:start] + '\\ ', None, None, None
            d.readline()
            offset = d.skipwhitespace(end)
            continue

        if token == '\\#':
            yield d.data[offset:start] + '#', None, None, None
        elif token.startswith('\\'):
            if token[1:] in tokenlist.tlist:
                yield d.data[offset:start + 1], token[1:], start + 1, end
            else:
                yield d.data[offset:end], None, None, None
        else:
            yield d.data[offset:start], token, start, end

        offset = end

def itercommandchars(d, offset, tokenlist):
    s = tokenlist.continuationre

    while offset < len(d.data):
        m = s.search(d.data, pos=offset)
        if m is None:
            yield d.data[offset:], None, None, None
            return

        token = m.group(0)
        start = m.start(0)
        end = m.end(0)

        if token == '\n':
            assert end == len(d.data)
            yield d.data[offset:start], None, None, None
            return

        if token == '\\\n':
            yield d.data[offset:end], None, None, None
            d.readline()
            offset = end
            if offset < len(d.data) and d.data[offset] == '\t':
                offset += 1
            continue
        
        if token.startswith('\\'):
            if token[1:] in tokenlist.tlist:
                yield d.data[offset:start + 1], token[1:], start + 1, end
            else:
                yield d.data[offset:end], None, None, None
        else:
            yield d.data[offset:start], token, start, end

        offset = end

definestokenlist = TokenList.get(('define', 'endef'))

def iterdefinechars(d, offset, tokenlist):
    """
    A Data generator yielding (char, offset). It will process define/endef
    according to define parsing rules.
    """

    def checkfortoken(o):
        """
        Check for a define or endef token on the line starting at o.
        Return an integer for the direction of definecount.
        """
        if o >= len(d.data):
            return 0

        if d.data[o] == '\t':
            return 0

        o = d.skipwhitespace(o)
        token, o = d.findtoken(o, definestokenlist, True)
        if token == 'define':
            return 1

        if token == 'endef':
            return -1
        
        return 0

    startoffset = offset
    definecount = 1 + checkfortoken(offset)
    if definecount == 0:
        return

    s = tokenlist.continuationre

    while offset < len(d.data):
        m = s.search(d.data, pos=offset)
        if m is None:
            yield d.data[offset:], None, None, None
            break

        token = m.group(0)
        start = m.start(0)
        end = m.end(0)

        if token == '\\\n':
            yield d.data[offset:start].rstrip() + ' ', None, None, None
            d.readline()
            offset = d.skipwhitespace(end)
            continue

        if token == '\n':
            assert end == len(d.data)
            d.readline()
            definecount += checkfortoken(end)
            if definecount == 0:
                yield d.data[offset:start], None, None, None
                return

            yield d.data[offset:end], None, None, None
        elif token.startswith('\\'):
            if token[1:] in tokenlist.tlist:
                yield d.data[offset:start + 1], token[1:], start + 1, end
            else:
                yield d.data[offset:end], None, None, None
        else:
            yield d.data[offset:start], token, start, end

        offset = end

    # Unlike the other iterators, if you fall off this one there is an unterminated
    # define.
    raise SyntaxError("Unterminated define", d.getloc(startoffset))

def _iterflatten(iter, data, offset):
    return ''.join((str for str, t, o, oo in iter(data, offset, emptytokenlist)))

def ensureend(d, offset, msg, ifunc=itermakefilechars):
    """
    Ensure that only whitespace remains in this data.
    """

    for c, t, o, oo in ifunc(d, offset, emptytokenlist):
        if c != '' and not c.isspace():
            raise SyntaxError(msg, d.getloc(o))

def iterlines(fd):
    """Yield (lineno, line) for each line in fd"""

    lineno = 0
    for line in fd:
        lineno += 1

        if line.endswith('\r\n'):
            line = line[:-2] + '\n'

        yield (lineno, line)

eqargstokenlist = TokenList.get(('(', "'", '"'))

def ifeq(d, offset):
    # the variety of formats for this directive is rather maddening
    token, offset = d.findtoken(offset, eqargstokenlist, False)
    if token is None:
        raise SyntaxError("No arguments after conditional", d.getloc(offset))

    if token == '(':
        arg1, t, offset = parsemakesyntax(d, offset, (',',), itermakefilechars)
        if t is None:
            raise SyntaxError("Expected two arguments in conditional", d.getloc(offset))

        arg1.rstrip()

        offset = d.skipwhitespace(offset)
        arg2, t, offset = parsemakesyntax(d, offset, (')',), itermakefilechars)
        if t is None:
            raise SyntaxError("Unexpected text in conditional", d.getloc(offset))

        ensureend(d, offset, "Unexpected text after conditional")
    else:
        arg1, t, offset = parsemakesyntax(d, offset, (token,), itermakefilechars)
        if t is None:
            raise SyntaxError("Unexpected text in conditional", d.getloc(offset))

        offset = d.skipwhitespace(offset)
        if offset == len(d.data):
            raise SyntaxError("Expected two arguments in conditional", d.getloc(offset))

        token = d.data[offset]
        if token not in '\'"':
            raise SyntaxError("Unexpected text in conditional", d.getloc(offset))

        arg2, t, offset = parsemakesyntax(d, offset + 1, (token,), itermakefilechars)

        ensureend(d, offset, "Unexpected text after conditional")

    return parserdata.EqCondition(arg1, arg2)

def ifneq(d, offset):
    c = ifeq(d, offset)
    c.expected = False
    return c

def ifdef(d, offset):
    e, t, offset = parsemakesyntax(d, offset, (), itermakefilechars)
    e.rstrip()

    return parserdata.IfdefCondition(e)

def ifndef(d, offset):
    c = ifdef(d, offset)
    c.expected = False
    return c

conditionkeywords = {
    'ifeq': ifeq,
    'ifneq': ifneq,
    'ifdef': ifdef,
    'ifndef': ifndef
    }

conditiontokens = tuple(conditionkeywords.iterkeys())
directivestokenlist = TokenList.get(conditiontokens + \
    ('else', 'endif', 'define', 'endef', 'override', 'include', '-include', 'vpath', 'export', 'unexport'))
conditionkeywordstokenlist = TokenList.get(conditiontokens)

varsettokens = (':=', '+=', '?=', '=')

_parsecache = {} # realpath -> (mtime, Statements)

def parsefile(pathname):
    pathname = os.path.realpath(pathname)

    mtime = os.path.getmtime(pathname)

    if pathname in _parsecache:
        oldmtime, stmts = _parsecache[pathname]

        if mtime == oldmtime:
            log.debug("Using '%s' from the parser cache.", pathname)
            return stmts

        log.debug("Not using '%s' from the parser cache, mtimes don't match: was %s, now %s" % (pathname, oldmtime, mtime))

    stmts = parsestream(open(pathname), pathname)
    _parsecache[pathname] = mtime, stmts
    return stmts

def parsestream(fd, filename):
    """
    Parse a stream of makefile into a parser data structure.

    @param fd A file-like object containing the makefile data.
    """

    currule = False
    condstack = [parserdata.StatementList()]

    fdlines = iterlines(fd)

    while True:
        assert len(condstack) > 0

        d = DynamicData(fdlines, filename)
        if not d.readline():
            break

        if len(d.data) > 0 and d.data[0] == '\t' and currule:
            e, t, o = parsemakesyntax(d, 1, (), itercommandchars)
            assert t == None
            condstack[-1].append(parserdata.Command(e))
        else:
            # To parse Makefile syntax, we first strip leading whitespace and
            # look for initial keywords. If there are no keywords, it's either
            # setting a variable or writing a rule.

            offset = d.skipwhitespace(0)

            kword, offset = d.findtoken(offset, directivestokenlist, True)
            if kword == 'endif':
                ensureend(d, offset, "Unexpected data after 'endif' directive")
                if len(condstack) == 1:
                    raise SyntaxError("unmatched 'endif' directive",
                                      d.getloc(offset))

                condstack.pop()
                continue
            
            if kword == 'else':
                if len(condstack) == 1:
                    raise SyntaxError("unmatched 'else' directive",
                                      d.getloc(offset))

                kword, offset = d.findtoken(offset, conditionkeywordstokenlist, True)
                if kword is None:
                    ensureend(d, offset, "Unexpected data after 'else' directive.")
                    condstack[-1].addcondition(d.getloc(offset), parserdata.ElseCondition())
                else:
                    if kword not in conditionkeywords:
                        raise SyntaxError("Unexpected condition after 'else' directive.",
                                          d.getloc(offset))

                    c = conditionkeywords[kword](d, offset)
                    condstack[-1].addcondition(d.getloc(offset), c)
                continue

            if kword in conditionkeywords:
                c = conditionkeywords[kword](d, offset)
                cb = parserdata.ConditionBlock(d.getloc(0), c)
                condstack[-1].append(cb)
                condstack.append(cb)
                continue

            if kword == 'endef':
                raise SyntaxError("Unmatched endef", d.getloc(offset))

            if kword == 'define':
                currule = False
                vname, t, i = parsemakesyntax(d, offset, (), itermakefilechars)

                d = DynamicData(fdlines, filename)
                d.readline()

                value = _iterflatten(iterdefinechars, d, 0)
                condstack[-1].append(parserdata.SetVariable(vname, value=value, valueloc=d.getloc(0), token='=', targetexp=None))
                continue

            if kword in ('include', '-include'):
                currule = False
                incfile, t, offset = parsemakesyntax(d, offset, (), itermakefilechars)
                condstack[-1].append(parserdata.Include(incfile, kword == 'include'))
                continue

            if kword == 'vpath':
                currule = False
                e, t, offset = parsemakesyntax(d, offset, (), itermakefilechars)
                condstack[-1].append(parserdata.VPathDirective(e))
                continue

            if kword == 'override':
                currule = False
                vname, token, offset = parsemakesyntax(d, offset, varsettokens, itermakefilechars)
                vname.lstrip()
                vname.rstrip()

                if token is None:
                    raise SyntaxError("Malformed override directive, need =", d.getloc(offset))

                value = _iterflatten(itermakefilechars, d, offset).lstrip()

                condstack[-1].append(parserdata.SetVariable(vname, value=value, valueloc=d.getloc(offset), token=token, targetexp=None, source=data.Variables.SOURCE_OVERRIDE))
                continue

            if kword == 'export':
                currule = False
                e, token, offset = parsemakesyntax(d, offset, varsettokens, itermakefilechars)
                e.lstrip()
                e.rstrip()

                if token is None:
                    condstack[-1].append(parserdata.ExportDirective(e, single=False))
                else:
                    condstack[-1].append(parserdata.ExportDirective(e, single=True))

                    value = _iterflatten(itermakefilechars, d, offset).lstrip()
                    condstack[-1].append(parserdata.SetVariable(e, value=value, valueloc=d.getloc(offset), token=token, targetexp=None))

                continue

            if kword == 'unexport':
                raise SyntaxError("unexporting variables is not supported", d.getloc(offset))

            assert kword is None, "unexpected kword: %r" % (kword,)

            e, token, offset = parsemakesyntax(d, offset, varsettokens + ('::', ':'), itermakefilechars)
            if token is None:
                condstack[-1].append(parserdata.EmptyDirective(e))
                continue

            # if we encountered real makefile syntax, the current rule is over
            currule = False

            if token in varsettokens:
                e.lstrip()
                e.rstrip()

                value = _iterflatten(itermakefilechars, d, offset).lstrip()

                condstack[-1].append(parserdata.SetVariable(e, value=value, valueloc=d.getloc(offset), token=token, targetexp=None))
            else:
                doublecolon = token == '::'

                # `e` is targets or target patterns, which can end up as
                # * a rule
                # * an implicit rule
                # * a static pattern rule
                # * a target-specific variable definition
                # * a pattern-specific variable definition
                # any of the rules may have order-only prerequisites
                # delimited by |, and a command delimited by ;
                targets = e

                e, token, offset = parsemakesyntax(d, offset,
                                                   varsettokens + (':', '|', ';'),
                                                   itermakefilechars)
                if token in (None, ';'):
                    condstack[-1].append(parserdata.Rule(targets, e, doublecolon))
                    currule = True

                    if token == ';':
                        offset = d.skipwhitespace(offset)
                        e, t, offset = parsemakesyntax(d, offset, (), itercommandchars)
                        condstack[-1].append(parserdata.Command(e))

                elif token in varsettokens:
                    e.lstrip()
                    e.rstrip()

                    value = _iterflatten(itermakefilechars, d, offset).lstrip()
                    condstack[-1].append(parserdata.SetVariable(e, value=value, valueloc=d.getloc(offset), token=token, targetexp=targets))
                elif token == '|':
                    raise SyntaxError('order-only prerequisites not implemented', d.getloc(offset))
                else:
                    assert token == ':'
                    # static pattern rule

                    pattern = e

                    deps, token, offset = parsemakesyntax(d, offset, (';',), itermakefilechars)

                    condstack[-1].append(parserdata.StaticPatternRule(targets, pattern, deps, doublecolon))
                    currule = True

                    if token == ';':
                        offset = d.skipwhitespace(offset)
                        e, token, offset = parsemakesyntax(d, offset, (), itercommandchars)
                        condstack[-1].append(parserdata.Command(e))

    if len(condstack) != 1:
        raise SyntaxError("Condition never terminated with endif", condstack[-1].loc)

    return condstack[0]

PARSESTATE_TOPLEVEL = 0    # at the top level
PARSESTATE_FUNCTION = 1    # expanding a function call. data is function

# For the following three, data is a tuple of Expansions: (varname, substfrom, substto)
PARSESTATE_VARNAME = 2     # expanding a variable expansion.
PARSESTATE_SUBSTFROM = 3   # expanding a variable expansion substitution "from" value
PARSESTATE_SUBSTTO = 4     # expanding a variable expansion substitution "to" value

PARSESTATE_PARENMATCH = 5

class ParseStackFrame(object):
    def __init__(self, parsestate, expansion, tokenlist, openbrace, closebrace, **kwargs):
        self.parsestate = parsestate
        self.expansion = expansion
        self.tokenlist = tokenlist
        self.openbrace = openbrace
        self.closebrace = closebrace
        for key, value in kwargs.iteritems():
            setattr(self, key, value)

_functiontokenlist = None

_matchingbrace = {
    '(': ')',
    '{': '}',
    }

def parsemakesyntax(d, startat, stopon, iterfunc):
    """
    Given Data, parse it into a data.Expansion.

    @param stopon (sequence)
        Indicate characters where toplevel parsing should stop.

    @param iterfunc (generator function)
        A function which is used to iterate over d, yielding (char, offset, loc)
        @see iterdata
        @see itermakefilechars
        @see itercommandchars
 
    @return a tuple (expansion, token, offset). If all the data is consumed,
    token and offset will be None
    """

    # print "parsemakesyntax(%r)" % d.data

    global _functiontokenlist
    if _functiontokenlist is None:
        functiontokens = list(functions.functionmap.iterkeys())
        functiontokens.sort(key=len, reverse=True)
        _functiontokenlist = TokenList.get(tuple(functiontokens))

    assert callable(iterfunc)

    stack = [
        ParseStackFrame(PARSESTATE_TOPLEVEL, data.Expansion(loc=d.getloc(startat)),
                        tokenlist=TokenList.get(stopon + ('$',)),
                        stopon=stopon, openbrace=None, closebrace=None)
    ]

    di = iterfunc(d, startat, stack[-1].tokenlist)
    while True: # this is not a for loop because `di` changes during the function
        stacktop = stack[-1]
        try:
            s, token, tokenoffset, offset = di.next()
        except StopIteration:
            break

        stacktop.expansion.append(s)
        if token is None:
            continue

        if token == '$':
            if len(d.data) == offset:
                # an unterminated $ expands to nothing
                break

            loc = d.getloc(tokenoffset)

            c = d.data[offset]
            if c == '$':
                stacktop.expansion.append('$')
                offset = offset + 1
            elif c in ('(', '{'):
                closebrace = _matchingbrace[c]

                # look forward for a function name
                fname, offset = d.findtoken(offset + 1, _functiontokenlist, True)
                if fname is not None:
                    fn = functions.functionmap[fname](loc)
                    e = data.Expansion()
                    fn.append(e)
                    if len(fn) == fn.maxargs:
                        tokenlist = TokenList.get((c, closebrace, '$'))
                    else:
                        tokenlist = TokenList.get((',', c, closebrace, '$'))

                    stack.append(ParseStackFrame(PARSESTATE_FUNCTION,
                                                 e, tokenlist, function=fn,
                                                 openbrace=c, closebrace=closebrace))
                    di = iterfunc(d, offset, tokenlist)
                    continue

                e = data.Expansion()
                tokenlist = TokenList.get((':', c, closebrace, '$'))
                stack.append(ParseStackFrame(PARSESTATE_VARNAME, e, tokenlist,
                                             openbrace=c, closebrace=closebrace, loc=loc))
                di = iterfunc(d, offset, tokenlist)
                continue
            else:
                e = data.Expansion.fromstring(c)
                stacktop.expansion.append(functions.VariableRef(loc, e))
                offset += 1
        elif token in ('(', '{'):
            assert token == stacktop.openbrace

            stacktop.expansion.append(token)
            stack.append(ParseStackFrame(PARSESTATE_PARENMATCH,
                                         stacktop.expansion,
                                         TokenList.get((token, stacktop.closebrace,)),
                                         openbrace=token, closebrace=stacktop.closebrace, loc=d.getloc(tokenoffset)))
        elif stacktop.parsestate == PARSESTATE_PARENMATCH:
            assert token == stacktop.closebrace
            stacktop.expansion.append(token)
            stack.pop()
        elif stacktop.parsestate == PARSESTATE_TOPLEVEL:
            assert len(stack) == 1
            return stacktop.expansion, token, offset
        elif stacktop.parsestate == PARSESTATE_FUNCTION:
            if token == ',':
                stacktop.expansion = data.Expansion()
                stacktop.function.append(stacktop.expansion)

                if len(stacktop.function) == stacktop.function.maxargs:
                    tokenlist = TokenList.get((stacktop.openbrace, stacktop.closebrace, '$'))
                    stacktop.tokenlist = tokenlist
            elif token in (')', '}'):
                    stacktop.function.setup()
                    stack.pop()
                    stack[-1].expansion.append(stacktop.function)
            else:
                assert False, "Not reached, PARSESTATE_FUNCTION"
        elif stacktop.parsestate == PARSESTATE_VARNAME:
            if token == ':':
                stacktop.varname = stacktop.expansion
                stacktop.parsestate = PARSESTATE_SUBSTFROM
                stacktop.expansion = data.Expansion()
                stacktop.tokenlist = TokenList.get(('=', stacktop.openbrace, stacktop.closebrace, '$'))
            elif token in (')', '}'):
                stack.pop()
                stack[-1].expansion.append(functions.VariableRef(stacktop.loc, stacktop.expansion))
            else:
                assert False, "Not reached, PARSESTATE_VARNAME"
        elif stacktop.parsestate == PARSESTATE_SUBSTFROM:
            if token == '=':
                stacktop.substfrom = stacktop.expansion
                stacktop.parsestate = PARSESTATE_SUBSTTO
                stacktop.expansion = data.Expansion()
                stacktop.tokenlist = TokenList.get((stacktop.openbrace, stacktop.closebrace, '$'))
            elif token in (')', '}'):
                # A substitution of the form $(VARNAME:.ee) is probably a mistake, but make
                # parses it. Issue a warning. Combine the varname and substfrom expansions to
                # make the compatible varname. See tests/var-substitutions.mk SIMPLE3SUBSTNAME
                log.warning("%s: Variable reference looks like substitution without =" % (stacktop.loc, ))
                stacktop.varname.append(':')
                stacktop.varname.concat(stacktop.expansion)
                stack.pop()
                stack[-1].expansion.append(functions.VariableRef(stacktop.loc, stacktop.varname))
            else:
                assert False, "Not reached, PARSESTATE_SUBSTFROM"
        elif stacktop.parsestate == PARSESTATE_SUBSTTO:
            assert token in  (')','}'), "Not reached, PARSESTATE_SUBSTTO"

            stack.pop()
            stack[-1].expansion.append(functions.SubstitutionRef(stacktop.loc, stacktop.varname,
                                                                 stacktop.substfrom, stacktop.expansion))
        else:
            assert False, "Unexpected parse state %s" % stacktop.parsestate

        di = iterfunc(d, offset, stack[-1].tokenlist)

    if len(stack) != 1:
        raise SyntaxError("Unterminated function call", d.getloc(offset))

    assert stack[0].parsestate == PARSESTATE_TOPLEVEL

    return stack[0].expansion, None, None
