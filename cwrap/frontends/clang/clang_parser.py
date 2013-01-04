#!/usr/bin/python
# -*- coding: utf-8 -*-

#------------------------------------------------------------------------------
# This file is adapted from ctypeslib.codegen.gccxmlparser
#------------------------------------------------------------------------------

#originally Thomas Heller, MIT license

import os
import sys
import re

import c_ast

#TODO: find better way to locate libclang library
import clang, clang.cindex
libpath, foo = os.path.split(clang.cindex.__file__)
clang.cindex.Config.set_library_path(libpath)
#put libclang.dylib(.so, .dll) into clang package directory)

from clang.cindex import CursorKind, TypeKind

# A function show(level, *args) would have been simpler but less fun
# and you'd need a separate parameter for the AST walkers if you want
# it to be exchangeable.
class Level(int):
    '''represent currently visited level of a tree'''
    def show(self, *args):
        '''pretty print an indented line'''
        print '\t'*self + ' '.join(map(str, args))
    def __add__(self, inc):
        '''increase level'''
        return Level(super(Level, self).__add__(inc))


def MAKE_NAME(name):
    """ Converts a mangled C++ name to a valid python identifier.

    """
    name = name.replace('$', 'DOLLAR')
    name = name.replace('.', 'DOT')
    if name.startswith('__'):
        return '_X' + name
    elif name[0] in '01234567879':
        return '_' + name
    return name


WORDPAT = re.compile('^[a-zA-Z_][a-zA-Z0-9_]*$')


def CHECK_NAME(name):
    """ Checks if `name` is a valid Python identifier. Returns
    `name` on success, None on failure.

    """
    if WORDPAT.match(name):
        return name
    return None


class ClangParser(object):
    """ Parses source file into a list of file-level c_ast nodes.

    """
    # cursor element types that have subelements.

    # For example, 
    # function arguments are subelements of a function, but struct 
    # fields are their own toplevel xml elements
    
    ##TODO
    #has_subelements = set(['Enumeration', 'Function', 'FunctionType',
    #                       'OperatorFunction', 'Method', 'Constructor',
    #                       'Destructor', 'OperatorMethod'])

    def __init__(self, *args):
        # `context` acts like stack where parent nodes are pushed
        # before visiting children
        self.context = []

        # `all` maps the unique ids from the xml to the c_ast
        # node that was generated by the element. This is used
        # after all nodes have been generated to go back and
        # hook up dependent nodes.
        self.all = {}

        ## collected nodes in parse order
        #self.nodes = []

        # XXX - what does this do?
        self.cpp_data = {}

        # `cdata` is used as temporary storage while elements
        # are being processed.
        self.cdata = None

        # `cvs_revision` stores the gccxml version in use.
        self.cvs_revision = None

    #--------------------------------------------------------------------------
    # Parsing entry points
    #--------------------------------------------------------------------------
    def parse(self, cfile):
        """ Parsing entry point. `cfile` is a filename or a file
        object.

        """

        index = clang.cindex.Index.create()
        tu = index.parse(cfile,
                         options = clang.cindex.TranslationUnit.PARSE_INCOMPLETE + \
                             clang.cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD + \
                             clang.cindex.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES
                         )

        for d in tu.diagnostics:
            self.print_diag_info(d)
        
        #UGLY: first element is TRANSLATION_UNIT, parse children
        self.parse_element(tu.cursor) 
        #for c in tu.cursor.get_children():
        #    self.parse_element(c)


    def print_diag_info(self, diag):
        print 'category name:', diag.category_name
        print 'location:', diag.location.file, diag.location.line, ':', diag.location.column
        print 'severity:', diag.severity
        print 'spelling:', diag.spelling
        #print 'ranges:', list(diag.ranges)
        #print 'fixits', list(diag.fixits)
        print 'fixits', ['%d:%d-%d:%d %s'%(f.range.start.line, f.range.start.column, 
                                           f.range.end.line, f.range.end.column,
                                           f.value) for f in diag.fixits]
        print


    simple_types = {TypeKind.VOID: 'void',
                    #TypeKind.BOOL = TypeKind(3)
                    TypeKind.CHAR_U: 'char',
                    TypeKind.UCHAR: 'char', #TODO unsigned? char????
                    #TypeKind.CHAR16 = TypeKind(6)
                    #TypeKind.CHAR32 = TypeKind(7)
                    TypeKind.USHORT: 'unsigned short int',
                    TypeKind.UINT: 'unsigned int',
                    TypeKind.ULONG: 'unsigned long int',
                    TypeKind.ULONGLONG: 'unsigned long long int',
                    #TypeKind.UINT128: TypeKind(12)
                    TypeKind.CHAR_S: 'signed char',
                    TypeKind.SCHAR: 'signed char',
                    #TypeKind.WCHAR: TypeKind(15)
                    TypeKind.SHORT: 'short int',
                    TypeKind.INT: 'int',
                    TypeKind.LONG: 'long int',
                    TypeKind.LONGLONG: 'long long int',
                    #TypeKind.INT128: TypeKind(20)
                    TypeKind.FLOAT: 'float',
                    TypeKind.DOUBLE: 'double',
                    TypeKind.LONGDOUBLE: 'long double',
                    }

    def type_to_c_ast_type(self, t, level, recurse = True):
        #convert clang type to c_ast type, return c_ast and hash value for corresponding cursor (or None)
        level.show( 'in type to c_ast:', 'kind', t.kind, t.get_declaration().spelling)

        kind = t.kind
        if kind in self.simple_types:
            return c_ast.FundamentalType(self.simple_types[kind]), None

        elif kind is TypeKind.CONSTANTARRAY:
            a, foo = self.type_to_c_ast_type(t.element_type, level+1)
            return c_ast.ArrayType(a, 0, t.element_count-1), None

        elif kind is TypeKind.TYPEDEF:
            return c_ast.FundamentalType(t.get_declaration().spelling), None #t.get_declaration().hash

        elif kind is TypeKind.POINTER:
            ptrtype, foo = self.type_to_c_ast_type(t.get_pointee(), level+1)
            if ptrtype is not None:
                return c_ast.PointerType(#self.type_to_c_ast_type(t.get_pointee()),
                    ptrtype, None, None), None

        elif kind is TypeKind.ENUM:
            #see if declaration already parsed
            typ = self.all.get(t.get_declaration().hash)
            if typ is not None:
                return typ, t.get_declaration().hash
            else:
                level.show('enum declaration not yet parsed')
                typ = self.parse_element(t.get_declaration(), level) #TODO ????
                return typ, t.get_declaration().hash

        elif kind is TypeKind.FUNCTIONPROTO:
            level.show('return type:')
            returntype, id_ = self.type_to_c_ast_type(t.get_result(), level+1)
            #TODO: very similar to visit_FUNCTION_DECL
            functype = c_ast.FunctionType(returntype, None)
            level.show('argument types:')
            for arg in t.argument_types():
                #TODO: argument name?
                functype.add_argument(c_ast.Argument('', self.type_to_c_ast_type(arg, level+1)[0]))
            return functype, None
            
        elif kind is TypeKind.UNEXPOSED and recurse:
            return self.type_to_c_ast_type(t.get_canonical(), level+1, recurse = False)
        
        else:
            level.show('do not know to handle type kind, search for declaration')
            typ = self.all.get(t.get_declaration().hash)

            #print 'in type_to_c_ast_type:'
            #print 'parsed type', typ
            #print

            if typ is not None:
                return typ, t.get_declaration().hash
            else:
                level.show("can't find declaration for type, parse type declaration", kind, t.get_declaration().kind)
                #print
                typ = self.parse_element(t.get_declaration(), level+1)
                if typ is not None:
                    return typ, t.get_declaration().hash
                else:
                    return c_ast.FundamentalType('unknown_type'), None #TODO: fixme
        

    def parse_element(self, cursor, level = Level()):
        
        #level.show('file:', repr(cursor.location.file))
        # ignore builtin nodes
        if cursor.location.file is None and cursor.kind is not CursorKind.TRANSLATION_UNIT:
            return

        # Find and call visitor
        mth = getattr(self, 'visit_' + cursor.kind.name, None)
        if mth is not None:
            result = mth(cursor, level)
        else:
            result = self.unhandled_element(cursor, level)
        
        # Record the result and register the the id, which is
        # used in the _fixup_* methods. Some elements don't have
        # an id, so we create our own.
        if result is not None:
            location = cursor.location
            if location.file is not None:
                result.location = (location.file.name, location.line)
            
            #self.nodes.append(result)
            
            #TODO: remove, not necessary???
            _id = cursor.hash
            if _id is not None:
                self.all[_id] = result
            #else:
            #    self.all[id(result)] = result

        #debug output
        if result is not None:
            level.show('cursor:', cursor.kind, str(cursor.type.kind))
            level.show('name:', repr(result.name))
            print
        

        # if this element has subelements, push it onto the context
        # since the next elements will be it's children.
        if cursor.kind in [CursorKind.TRANSLATION_UNIT,
                           CursorKind.ENUM_DECL,
                           CursorKind.STRUCT_DECL,
                           CursorKind.UNION_DECL,
                           #CursorKind.FUNCTION_DECL,
                           #CursorKind.MACRO_DEFINITION,
                           ]:
            self.context.append(result)

            for c in cursor.get_children():
                child = self.parse_element(c, level+1)
                result.add_child(child)
                
            # if this element has subelements, then it will have
            # been push onto the stack and needs to be removed.
            self.context.pop()

        self.cdata = None
        
        
        #level.show('parse_element result', result)
        #if result is not None:
        #    level.show(str(result.__dict__)) #.__dict__
        #print

        return result


    
    def unhandled_element(self, cursor, level):
        """ Handler for element nodes where a real handler is not
         found.

        """

        #level.show('file:', repr(cursor.location.file))
        # ignore builtin nodes
        if cursor.location.file is None:
            return

        #print 'Unhandled element `%s`.' % cursor.displayname
        level.show('unhandled element', repr(cursor.spelling), repr(cursor.displayname), cursor.kind)
        #print

    #--------------------------------------------------------------------------
    # Ignored elements and do-nothing handlers
    #--------------------------------------------------------------------------
    def visit_Ignored(self, attrs):
        """ Ignored elements are those which we don't care about,
        but need to keep in place because we care about their 
        children.

        """
        name = attrs.get('name', None)
        if name is None:
            name = attrs.get('mangled', None)
            if name is None:
                name = 'UNDEFINED'
            else:
                name = MAKE_NAME(name)
        return c_ast.Ignored(name)

    visit_Method =  visit_Ignored
    visit_Constructor = visit_Ignored
    visit_Destructor = visit_Ignored
    visit_OperatorMethod  =  visit_Ignored
    #visit_Class = visit_Ignored
    visit_Base = visit_Ignored
    visit_Converter = visit_Ignored
    visit_MethodType = visit_Ignored

    # These node types are ignored becuase we don't need anything
    # at all from them.
    #visit_Class = lambda *args: None
    #visit_Base =  lambda *args: None
    visit_Ellipsis =  lambda *args: None

    visit_OffsetType = visit_Ignored

    # #--------------------------------------------------------------------------
    # # Revision Handler
    # #--------------------------------------------------------------------------
    # def visit_GCC_XML(self, attrs):
    #     """ Handles the versioning info from the gccxml version.

    #     """
    #     rev = attrs['cvs_revision']
    #     self.cvs_revision = tuple(map(int, rev.split('.')))
    
    # #--------------------------------------------------------------------------
    # # Text handlers
    # #--------------------------------------------------------------------------
    # def visit_Characters(self, content):
    #     """ The character handler which is called after each xml 
    #     element has been processed.

    #     """
    #     if self.cdata is not None:
    #         self.cdata.append(content)
    
    # def visit_CPP_DUMP(self, attrs):
    #     """ Gathers preprocessor elements like macros and defines.

    #     """
    #     # Insert a new list for each named section into self.cpp_data,
    #     # and point self.cdata to it.  self.cdata will be set to None
    #     # again at the end of each section.
    #     name = attrs['name']
    #     self.cpp_data[name] = self.cdata = []
 
    #--------------------------------------------------------------------------
    # Node element handlers
    #--------------------------------------------------------------------------
    def visit_TRANSLATION_UNIT(self, cursor, level):
        container = c_ast.File(cursor.displayname)
        self.context.append(container)
        return container

    def visit_TYPEDEF_DECL(self, cursor, level):
        c_ast_type, id_ = self.type_to_c_ast_type(cursor.underlying_typedef_type, level)
        if c_ast_type is not None:
            level.show('in visit_TYPEDEF_DECL, c_ast_type =', c_ast_type.__class__.__name__, 'name =', repr(c_ast_type.name))
            
            #special handling of typedef enum, struct, union
            if type(c_ast_type) in (c_ast.Enumeration, c_ast.Union, c_ast.Struct):
                if not c_ast_type.name: 
                    #unnamed record -> remove declaration from self.all 
                    level.show('remove declaration', c_ast_type, self.all[id_])
                    idx = c_ast_type.context.members.index(c_ast_type)
                    level.show('remove from parent, idx', idx)
                    c_ast_type.context.members.pop(idx)

                elif c_ast_type.name == cursor.spelling:
                    #enum tagname == typename: no typedef, do nothing
                    return
            
            return c_ast.Typedef(cursor.spelling, c_ast_type, None)
        
    def visit_STRUCT_DECL(self, cursor, level):
        name = cursor.spelling
        s = c_ast.Struct(name, context = self.context[-1], members = [])
        return s

    def visit_UNION_DECL(self, cursor, level):
        name = cursor.spelling
        return c_ast.Union(name, context = self.context[-1])

    def visit_FIELD_DECL(self, cursor, level):
        parent = self.context[-1]
        name = cursor.spelling
        c_ast_type, id_ = self.type_to_c_ast_type(cursor.type, level)
        member = c_ast.Field(name, c_ast_type, context = parent)
        return member
            
    def visit_ENUM_DECL(self, cursor, level):
        name = cursor.spelling
        return c_ast.Enumeration(name, self.context[-1])

    def visit_ENUM_CONSTANT_DECL(self, cursor, level):
        name = cursor.spelling
        value = cursor.enum_value
        return c_ast.EnumValue(name, value)
    
    def visit_FUNCTION_DECL(self, cursor, level):
        name = cursor.spelling
        returntype, id_ = self.type_to_c_ast_type(cursor.type.get_result(), level)
        func = c_ast.Function(name, returntype)
        for arg in cursor.get_arguments():
            level.show('function argument', arg.kind, arg.spelling)
            func.add_argument(c_ast.Argument(arg.spelling, self.type_to_c_ast_type(arg.type, level+1)[0]))
        return func

    def visit_VAR_DECL(self, cursor, level):
        name = cursor.spelling
        typ, id_ = self.type_to_c_ast_type(cursor.type, level)
        return c_ast.Variable(name, typ, None, None)
    
    # def visit_PARM_DECL(self, cursor, level):
    #     name = cursor.spelling
    #     parent = self.context[-1]
    #     typ, id_ = self.type_to_c_ast_type(cursor.type, level)
    #     arg = c_ast.Argument(name, typ)
    #     parent.add_argument(arg)
    #     return arg
        
        
        

        








    def visit_Namespace(self, attrs):
        name = attrs['name']
        members = attrs['members'].split()
        return c_ast.Namespace(name, members)
    
    def visit_File(self, attrs):
        name = attrs['name']
        return c_ast.File(name)

    def visit_Variable(self, attrs):
        name = attrs['name']
        typ = attrs['type']
        context = attrs['context']
        init = attrs.get('init', None)
        return c_ast.Variable(name, typ, context, init)

    def visit_PointerType(self, attrs):
        typ = attrs['type']
        size = attrs['size']
        align = attrs['align']
        return c_ast.PointerType(typ, size, align)

    visit_ReferenceType = visit_PointerType
   
    def visit_ArrayType(self, attrs):
        # min, max are the min and max array indices
        typ = attrs['type']
        min = attrs['min']
        max = attrs['max']
        if max == 'ffffffffffffffff':
            max = '-1'
        if max == '': #ADDED gregor
            max = '-1'
        min = int(min.rstrip('lu'))
        max = int(max.rstrip('lu'))
        return c_ast.ArrayType(typ, min, max)

    def visit_CvQualifiedType(self, attrs):
        typ = attrs['type']
        const = attrs.get('const', None)
        volatile = attrs.get('volatile', None)
        return c_ast.CvQualifiedType(typ, const, volatile)
 
    def visit_Function(self, attrs):
        name = attrs['name']
        returns = attrs['returns']
        context = attrs['context']
        attributes = attrs.get('attributes', '').split()
        extern = attrs.get('extern')
        return c_ast.Function(name, returns, context, attributes, extern)

    def visit_FunctionType(self, attrs):
        returns = attrs['returns']
        attributes = attrs.get('attributes', '').split()
        return c_ast.FunctionType(returns, attributes)
  
    def visit_OperatorFunction(self, attrs):
        name = attrs['name']
        returns = attrs['returns']
        context = attrs['context']
        attributes = attrs.get('attributes', '').split()
        extern = attrs.get('extern')
        #return c_ast.OperatorFunction(name, returns)
        return c_ast.OperatorFunction(name, returns, context, attributes, extern)

    def visit_Argument(self, attrs):
        parent = self.context[-1]
        if parent is not None:
            typ = attrs['type']
            name = attrs.get('name')
            arg = c_ast.Argument(typ, name)
            parent.add_argument(arg)

    def visit_Enumeration(self, attrs):
        # If the name isn't a valid Python identifier, 
        # create an unnamed enum
        name = CHECK_NAME(attrs['name'])
        size = attrs['size']
        align = attrs['align']
        return c_ast.Enumeration(name, size, align)

    
    def visit_EnumValue(self, attrs):
        parent = self.context[-1]
        if parent is not None:
            name = attrs['name']
            value = attrs['init']
            val = c_ast.EnumValue(name, value)
            parent.add_value(val)

    def visit_Struct(self, attrs):
        name = attrs.get('name')
        if name is None:
            name = MAKE_NAME(attrs['mangled'])
        bases = attrs.get('bases', '').split()
        members = attrs.get('members', '').split()
        context = attrs['context']
        align = attrs['align']
        size = attrs.get('size')
        return c_ast.Struct(name, align, members, context, bases, size)

    def visit_Class(self, attrs):
        name = attrs.get('name')
        if name is None:
            name = MAKE_NAME(attrs['mangled'])
        bases = attrs.get('bases', '').split()
        #fix 'protected:_12345'
        bases = [b.replace('protected:','') for b in bases]
        members = attrs.get('members', '').split()
        context = attrs['context']
        align = attrs['align']
        size = attrs.get('size')
        return c_ast.Struct(name, align, members, context, bases, size) #TODO: Class

    
    def visit_Union(self, attrs):
        name = attrs.get('name')
        if name is None:
            name = MAKE_NAME(attrs['mangled'])
        bases = attrs.get('bases', '').split()
        members = attrs.get('members', '').split()
        context = attrs['context']
        align = attrs['align']
        size = attrs.get('size')
        return c_ast.Union(name, align, members, context, bases, size)

    def visit_Field(self, attrs):
        name = attrs['name']
        typ = attrs['type']
        context = attrs['context']
        bits = attrs.get('bits', None)
        offset = attrs.get('offset')
        return c_ast.Field(name, typ, context, bits, offset)


    #visit_Class = visit_Struct
    #visit_Class = visit_Ignored


    #--------------------------------------------------------------------------
    # Fixup handlers
    #--------------------------------------------------------------------------

    # The fixup handlers use the ids save on the node attrs to lookup 
    # the replacement node from the storage, then do the swapout. There
    # must be a fixup handler (even if its pass-thru) for each node
    # handler that returns a node object.
    
    def _fixup_Namespace(self, ns):
        for i, mbr in enumerate(ns.members):
            ns.members[i] = self.all[mbr]

    def _fixup_File(self, f): 
        pass
    
    def _fixup_Variable(self, t):
        #t.typ = self.all[t.typ]
        #t.context = self.all[t.context]
        pass

    def _fixup_Typedef(self, t):
        #t.typ = self.all[t.typ]
        #t.context = self.all[t.context]
        pass

    def _fixup_FundamentalType(self, t): 
        pass

    def _fixup_PointerType(self, p):
        p.typ = self.all[p.typ]

    _fixup_ReferenceType = _fixup_PointerType

    def _fixup_ArrayType(self, a):
        #a.typ = self.all[a.typ]
        pass

    def _fixup_CvQualifiedType(self, c):
        c.typ = self.all[c.typ]

    def _fixup_Function(self, func):
        #func.returns = self.all[func.returns]
        #func.context = self.all[func.context]
        #func.fixup_argtypes(self.all)
        pass
        
    def _fixup_FunctionType(self, func):
        func.returns = self.all[func.returns]
        func.fixup_argtypes(self.all)
        
    def _fixup_OperatorFunction(self, func):
        func.returns = self.all[func.returns]
        func.context = self.all[func.context]
        func.fixup_argtypes(self.all)

    def _fixup_Enumeration(self, e): 
        pass

    def _fixup_EnumValue(self, e): 
        pass
    
    def _fixup_Struct(self, s):
        #s.members = [self.all[m] for m in s.members]
        #s.bases = [self.all[b] for b in s.bases]
        #s.context = self.all[s.context]
        pass

    def _fixup_Union(self, u):
        #u.members = [self.all[m] for m in u.members]
        #u.bases = [self.all[b] for b in u.bases]
        #u.context = self.all[u.context]
        pass

    def _fixup_Field(self, f):
        #f.typ = self.all[f.typ]
        #f.context = self.all[f.context]
        pass

    def _fixup_Macro(self, m):
        pass
    
    def _fixup_Ignored(self, const): 
        pass

    _fixup_Method = _fixup_Ignored
    _fixup_Constructor = _fixup_Ignored
    _fixup_Destructor = _fixup_Ignored
    _fixup_OperatorMethod = _fixup_Ignored
   
    #--------------------------------------------------------------------------
    # Post parsing helpers
    #--------------------------------------------------------------------------
    def get_macros(self, text):
        """ Attempts to extract the macros from a piece of text
        and converts it to a Macro node containing the name,
        args, and body.  

        """
        if text is None:
            return
        
        # join and split so we can accept a list or  string. 
        text = ''.join(text)
        for m in text.splitlines():
            name, body = m.split(None, 1)
            name, args = name.split('(', 1)
            args = '(%s' % args
            self.all[name] = c_ast.Macro(name, args, body)

    def get_aliases(self, text, namespace):
        """ Attemps to extract defined aliases of the form
        #define A B and store them in an Alias node.

        """
        if text is None:
            return
        
        aliases = {}
        text = ''.join(text)
        for a in text.splitlines():
            name, value = a.split(None, 1)
            a = c_ast.Alias(name, value)
            aliases[name] = a
            self.all[name] = a

        # The alias value will be located in the namespace,
        # or the aliases. Otherwise, it's unfound.
        for name, a in aliases.items():
            value = a.value
            if value in namespace:
                a.typ = namespace[value]
            elif value in aliases:
                a.typ = aliases[value]
            else:
                pass

    def get_result(self):
        """ After parsing, call this method to retrieve the results
        as a list of AST nodes. This list will contain *all* nodes
        in the xml file which will include a bunch of builtin and 
        internal stuff that you wont want.

        """
        
        # Gather any macros.
        self.get_macros(self.cpp_data.get('functions'))

        # Walk through all the items, hooking up the appropriate 
        # links by replacing the id tags with the actual objects
        remove = []
        for name, node in self.all.items():
            # location = getattr(node, 'location', None)
            # if location is not None:
            #     fil = location.file
            #     #line = location.line
            #     line = 0
            #     #node.location = (self.all[fil].name, int(line))
                
            method_name = '_fixup_' + node.__class__.__name__
            fixup_method = getattr(self, method_name, None)
            if fixup_method is not None:
                fixup_method(node)
            else:
                remove.append(node)
                print "remove node", node
        
        # # remove any nodes don't have handler methods
        # for n in remove:
        #     del self.all[n]
               
        # sub out any #define'd aliases and collect all the nodes 
        # we're interested in. The interesting nodes are not necessarily
        # all nodes, but rather the ones that may need to be modified
        # by the transformations applied later on.
        interesting = (c_ast.Typedef, c_ast.Struct, c_ast.Enumeration, 
                       c_ast.Union, c_ast.Function, c_ast.Variable, 
                       c_ast.Namespace, c_ast.File)

        result = []
        namespace = {}
        for node in self.all.values():
        #for node in self.nodes: #traverse results in parse order
            if not isinstance(node, interesting):
                continue
            name = getattr(node, 'name', None)
            if name is not None:
                namespace[name] = node
            result.append(node)
        self.get_aliases(self.cpp_data.get('aliases'), namespace)
        
        return result


def parse(cfile):
    # parse an XML file into a sequence of type descriptions
    parser = ClangParser()
    parser.parse(cfile)
    
    print 'all:'
    for a in parser.all:
        print hex(a), parser.all[a].name
    print

    items = parser.get_result()

    print 'in clang_parser.py/parse(), items:'
    for i in items:
        print "%20s: %s"%(i.__class__.__name__, i.name)

    return items
