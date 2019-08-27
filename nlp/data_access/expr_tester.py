#!/usr/bin/env python3
"""
This is a program for testing the ClarityNLP NLPQL expression evaluator.

It assumes that a run of the NLPQL file 'data_gen.nlpql' has already been
performed. You will need to know the job_id from that run to use this code.

Add your desired expression to the list in _run_tests, then evaluate it using
the data from your ClarityNLP run.
Use this command:

    python3 ./expr_tester.py --jobid <job_id> --mongohost <ip address>
                             --port <port number> --num <number> [--debug]


Help for the command line interface can be obtained via this command:

    python3 ./expr_tester.py --help

Extensive debugging info can be generated with the --debug option.

"""

import re
import os
import sys
import copy
import string
import argparse
import datetime
import subprocess
from pymongo import MongoClient
from collections import namedtuple, OrderedDict
#from bson import ObjectId

try:
    import expr_eval
    import expr_result
except:
    from data_access import expr_eval
    from data_access import expr_result
    
_VERSION_MAJOR = 0
_VERSION_MINOR = 6
_MODULE_NAME   = 'expr_tester.py'

_TRACE = False

_TEST_ID            = 'EXPR_TEST'
_TEST_NLPQL_FEATURE = 'EXPR_TEST'


_FILE_DATA_FIELDS = [
    'context',            # value of context variable in NLPQL file
    'names',              # all defined names in the NLPQL file
    'tasks',              # list of ClarityNLP tasks defined in the NLPQL file
    'primitives',         # names actually used in expressions
    'expressions',        # list of (nlpql_feature, string_def) tuples
    'reduced_expressions' # same but with string_def expressed with primitives
]
FileData = namedtuple('FileData', _FILE_DATA_FIELDS)

# names defined in the test data set
_NAME_LIST = [
    'hasRigors', 'hasDyspnea', 'hasNausea', 'hasVomiting', 'hasShock',
    'hasTachycardia', 'hasLesion', 'Temperature', 'Lesion',
    'hasFever', 'hasSepsisSymptoms', 'hasTempAndSepsisSymptoms',
    'hasSepsis', 'hasLesionAndSepsisSymptoms', 'hasLesionAndTemp',
    'hasLesionTempAndSepsisSymptoms'
]


###############################################################################
def _enable_debug():

    global _TRACE
    _TRACE = True


###############################################################################
def _evaluate_expressions(expr_obj_list,
                          mongo_collection_obj,
                          job_id,
                          context_field,
                          is_final):
    """
    Nearly identical to
    nlp/luigi_tools/phenotype_helper.mongo_process_operations
    """

    phenotype_id    = _TEST_ID
    phenotype_owner = _TEST_ID
        
    assert 'subject' == context_field or 'report_id' == context_field

    all_output_docs = []
    is_final_save = is_final

    for expr_obj in expr_obj_list:

        # the 'is_final' flag only applies to the last subexpression
        if expr_obj != expr_obj_list[-1]:
            is_final = False
        else:
            is_final = is_final_save
        
        # evaluate the (sub)expression in expr_obj
        eval_result = expr_eval.evaluate_expression(expr_obj,
                                                    job_id,
                                                    context_field,
                                                    mongo_collection_obj)
            
        # query MongoDB to get result docs
        cursor = mongo_collection_obj.find({'_id': {'$in': eval_result.doc_ids}})

        # initialize for MongoDB result document generation
        phenotype_info = expr_result.PhenotypeInfo(
            job_id = job_id,
            phenotype_id = phenotype_id,
            owner = phenotype_owner,
            context_field = context_field,
            is_final = is_final
        )

        # generate result documents
        if expr_eval.EXPR_TYPE_MATH == eval_result.expr_type:

            output_docs = expr_result.to_math_result_docs(eval_result,
                                                          phenotype_info,
                                                          cursor)
        else:
            assert expr_eval.EXPR_TYPE_LOGIC == eval_result.expr_type

            # flatten the result set into a set of Mongo documents
            doc_map, oid_list_of_lists = expr_eval.flatten_logical_result(eval_result,
                                                                          mongo_collection_obj)
            
            output_docs = expr_result.to_logic_result_docs(eval_result,
                                                           phenotype_info,
                                                           doc_map,
                                                           oid_list_of_lists)

        if len(output_docs) > 0:
            mongo_collection_obj.insert_many(output_docs)
        else:
            print('mongo_process_operations ({0}): ' \
                  'no phenotype matches on "{1}".'.format(eval_result.expr_type,
                                                          eval_result.expr_text))

        # save the expr object and the results
        all_output_docs.append( (expr_obj, output_docs))

    return all_output_docs


###############################################################################
def _delete_prev_results(job_id, mongo_collection_obj):
    """
    Remove all results in the Mongo collection that were computed by
    expression evaluation. Also remove all temp results from a previous run
    of this code, if any.
    """

    # delete all assigned results from a previous run of this code
    result = mongo_collection_obj.delete_many(
        {"job_id":job_id, "nlpql_feature":_TEST_NLPQL_FEATURE})
    print('Removed {0} result docs with the test feature.'.
          format(result.deleted_count))

    # delete all temp results from a previous run of this code
    result = mongo_collection_obj.delete_many(
        {"nlpql_feature":expr_eval.regex_temp_nlpql_feature})
    print('Removed {0} docs with temp NLPQL features.'.
          format(result.deleted_count))
    

###############################################################################
def banner_print(msg):
    """
    Print the message centered in a border of stars.
    """

    MIN_WIDTH = 79

    n = len(msg)
    
    if n < MIN_WIDTH:
        ws = (MIN_WIDTH - 2 - n) // 2
    else:
        ws = 1

    ws_left = ws
    ws_right = ws

    # add extra space on right to balance if even
    if 0 == n % 2:
        ws_right = ws+1

    star_count = 1 + ws_left + n + ws_right + 1
        
    print('{0}'.format('*'*star_count))
    print('{0}{1}{2}'.format('*', ' '*(star_count-2), '*'))
    print('{0}{1}{2}{3}{4}'.format('*', ' '*ws_left, msg, ' '*ws_right, '*'))
    print('{0}{1}{2}'.format('*', ' '*(star_count-2), '*'))
    print('{0}'.format('*'*star_count))
    

###############################################################################
def _run_selftest_expression(job_id,
                             context_field,
                             expression_str,
                             mongo_collection_obj):
    """
    Evaluate the NLPQL expression in 'expression_str', then iterate through
    the results and extract the set of unique context variables. A context
    variable is either the doc_id or the patient_id, depending on the NLPQL
    evaluation context. Returns the set of unique context variables.
    """

    parse_result = expr_eval.parse_expression(expression_str, _NAME_LIST)
    if 0 == len(parse_result):
        return set()

    # generate a list of ExpressionObject primitives
    expression_object_list = expr_eval.generate_expressions(_TEST_NLPQL_FEATURE,
                                                            parse_result)
    if 0 == len(expression_object_list):
        return set()

    # evaluate the ExpressionObjects in the list
    eval_results = _evaluate_expressions(expression_object_list,
                                         mongo_collection_obj,
                                         job_id,
                                         context_field,
                                         is_final=False)

    result_set = set()
    for expr_obj, docs in eval_results:
        for doc in docs:
            if _TEST_NLPQL_FEATURE == doc['nlpql_feature']:
                result_set.add(doc[context_field])
    
    return result_set


###############################################################################
def _to_context_set(context_field, docs):

    feature_set = set()
    for doc in docs:
        assert context_field in doc
        value = doc[context_field]
        feature_set.add(value)

    return feature_set


###############################################################################
def _get_feature_set(mongo_collection_obj, context_field, nlpql_feature):
    """
    Extract the set of all context variables (either doc or patient IDs)
    having the indicated feature.
    """

    docs = mongo_collection_obj.find({'nlpql_feature':nlpql_feature})
    return _to_context_set(context_field, docs)


###############################################################################
def _test_basic_expressions(job_id,     # integer job id from data file
                            cf,         # context field, 'document' or 'subject'
                            data,       # dict of basic query results
                            mongo_obj):

    # do simplest expressions first
    BASIC_EXPRESSIONS = [
        'Temperature', 'Lesion', 'hasTachycardia', 'hasRigors', 'hasShock',
        'hasDyspnea', 'hasNausea', 'hasVomiting'
    ]

    # rename some precomputed sets
    temp   = data['Temperature']
    lesion = data['Lesion']

    for expr in BASIC_EXPRESSIONS:
        computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
        expected = data[expr]
        if computed != expected:
            return False

    expr = 'Temperature AND Lesion'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = temp & lesion
    if computed != expected:
        return False

    # 'Temperature AND field', 'Lesion AND field'
    for field in BASIC_EXPRESSIONS[2:]:
        expr = 'Temperature AND {0}'.format(field)
        computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
        expected = temp & data[field]
        if computed != expected:
            return False

        expr = 'Lesion AND {0}'.format(field)
        computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
        expected = lesion & data[field]
        if computed != expected:
            return False

    # 'Temperature OR field', 'Lesion OR field'
    for field in BASIC_EXPRESSIONS[2:]:
        expr = 'Temperature OR {0}'.format(field)
        computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
        expected = temp | data[field]
        if computed != expected:
            return False

        expr = 'Lesion OR {0}'.format(field)
        computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
        expected = lesion | data[field]
        if computed != expected:
            return False

    return True


###############################################################################
def _test_pure_math_expressions(job_id,     # integer job id from data file
                                cf,         # context field, 'document' or 'subject'
                                mongo_obj):

    expr = 'Temperature.value >= 100.4'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            'nlpql_feature':'Temperature',
            'value': {'$gte': 100.4}
        })
    expected = _to_context_set(cf, docs)
    if computed != expected:
        return False

    expr = 'Temperature.value >= 1.004e2'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    if computed != expected:
        return False

    expr = '100.4 <= Temperature.value'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    if computed != expected:
        return False

    expr = '(Temperature.value >= (100.4))'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    if computed != expected:
        return False

    expr = 'Temperature.value == 100.4'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            'nlpql_feature':'Temperature',
            'value':100.4
        })
    expected = _to_context_set(cf, docs)
    if computed != expected:
        return False

    expr = 'Temperature.value + 3 ^ 2 < 109'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            'nlpql_feature':'Temperature',
            'value': {'$lt':100}
        })
    expected = _to_context_set(cf, docs)
    if computed != expected:
        return False

    expr = 'Temperature.value ^ 3 + 2 < 941194'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            'nlpql_feature':'Temperature',
            'value': {'$lt': 98}
        })
    expected = _to_context_set(cf, docs)
    if computed != expected:
        return False

    expr = 'Temperature.value % 3 ^ 2 == 2'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            'nlpql_feature':'Temperature',
            'value':101
        })
    expected = _to_context_set(cf, docs)
    if computed != expected:
        return False

    expr = 'Temperature.value * 4 ^ 2 >= 1616'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            'nlpql_feature':'Temperature',
            'value': {'$gte':101}
        })
    expected = _to_context_set(cf, docs)
    if computed != expected:
        return False

    expr = 'Temperature.value / 98.6 ^ 2 < 0.01'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            'nlpql_feature':'Temperature',
            'value': {'$lt':97.2196}
        })
    expected = _to_context_set(cf, docs)
    if computed != expected:
        return False

    expr = '(Temperature.value / 98.6)^2 < 1.02'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            'nlpql_feature':'Temperature',
            'value':{'$lt':99.581}
        })
    expected = _to_context_set(cf, docs)
    if computed != expected:
        return False

    expr = '0 == Temperature.value % 20'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            'nlpql_feature':'Temperature',
            'value':100
        })
    expected = _to_context_set(cf, docs)
    if computed != expected:
        return False

    expr = '(Lesion.dimension_X <= 5) OR (Lesion.dimension_X >= 45)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            'nlpql_feature':'Lesion',
            '$or':
            [
                {'dimension_X':{'$lte':5}},
                {'dimension_X':{'$gte':45}}
            ]
        })
    expected = _to_context_set(cf, docs)
    if computed != expected:
        return False

    expr = 'Lesion.dimension_X > 15 AND Lesion.dimension_X < 30'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            'nlpql_feature':'Lesion',
            '$and':
            [
                {'dimension_X':{'$gt':15}},
                {'dimension_X':{'$lt':30}}
            ]
        })
    expected = _to_context_set(cf, docs)
    if computed != expected:
        return False

    expr = '((Lesion.dimension_X) > (15)) AND (((Lesion.dimension_X) < (30)))'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    if computed != expected:
        return False

    # redundant math expressions
    
    expr = 'Lesion.dimension_X > 10 AND Lesion.dimension_X < 30'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            '$and':
            [
                {'nlpql_feature':'Lesion'},
                {'dimension_X':{'$gt':10}},
                {'dimension_X':{'$lt':30}}
            ]
        })
    expected = _to_context_set(cf, docs)
    if computed != expected:
        return False

    expr = 'Lesion.dimension_X > 5 AND Lesion.dimension_X > 10 AND ' \
        'Lesion.dimension_X < 40 AND Lesion.dimension_X < 30'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    if computed != expected:
        return False

    expr = '(Lesion.dimension_X > 8 AND Lesion.dimension_X > 5 AND ' \
        'Lesion.dimension_X > 10) AND (Lesion.dimension_X < 40 AND ' \
        'Lesion.dimension_X < 30 AND Lesion.dimension_X < 45)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    if computed != expected:
        return False

    
    return True


###############################################################################
def _test_math_with_multiple_features(job_id, cf, mongo_obj):

    expr = 'Lesion.dimension_X > 15 AND Lesion.dimension_X < 30 OR ' \
        '(Temperature.value >= 100.4)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            '$or':
            [
                {
                    '$and':
                    [
                        {'nlpql_feature':'Lesion'},
                        {'dimension_X':{'$gt':15}},
                        {'dimension_X':{'$lt':30}}
                    ]
                },
                {
                    '$and':
                    [
                        {'nlpql_feature':'Temperature'},
                        {'value':{'$gte':100.4}}
                    ]
                }
            ]
        })
    expected = _to_context_set(cf, docs)
    if computed != expected:
        return False

    expr = '(Lesion.dimension_X > 15 AND Lesion.dimension_X < 30) AND ' \
        'Temperature.value > 100.4'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs1 = mongo_obj.find(
        {
            '$and':
            [
                {'nlpql_feature':'Lesion'},
                {'dimension_X':{'$gt':15}},
                {'dimension_X':{'$lt':30}}
            ]
        })
    expected1 = _to_context_set(cf, docs1)
    docs2 = mongo_obj.find(
        {
            '$and':
            [
                {'nlpql_feature':'Temperature'},
                {'value':{'$gt':100.4}}
            ]
        })
    expected2 = _to_context_set(cf, docs2)
    expected = set.intersection(expected1, expected2)
    if computed != expected:
        return False

    expr = 'Lesion.dimension_X > 15 AND Lesion.dimension_X < 30 AND ' \
        'Temperature.value > 100.4'    
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    if computed != expected:
        return False

    expr = '(Temperature.value >= 102) AND (Lesion.dimension_X <= 5)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs1 = mongo_obj.find(
        {
            '$and':
            [
                {'nlpql_feature':'Temperature'},
                {'value':{'$gte':102}}
            ]
        })
    expected1 = _to_context_set(cf, docs1)
    docs2 = mongo_obj.find(
        {
            '$and':
            [
                {'nlpql_feature':'Lesion'},
                {'dimension_X':{'$lte':5}}
            ]
        })
    expected2 = _to_context_set(cf, docs2)
    expected = set.intersection(expected1, expected2)
    if computed != expected:
        return False

    expr = '(Temperature.value >= 102) AND (Lesion.dimension_X <= 5) AND ' \
        '(Temperature.value >= 103)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs1 = mongo_obj.find(
        {
            '$and':
            [
                {'nlpql_feature':'Temperature'},
                {'value':{'$gte':102}}
            ]
        })
    expected1 = _to_context_set(cf, docs1)
    docs2 = mongo_obj.find(
        {
            '$and':
            [
                {'nlpql_feature':'Lesion'},
                {'dimension_X':{'$lte':5}}
            ]
        })
    expected2 = _to_context_set(cf, docs2)
    docs3 = mongo_obj.find(
        {
            '$and':
            [
                {'nlpql_feature':'Temperature'},
                {'value':{'$gte':103}}
            ]
        })
    expected3 = _to_context_set(cf, docs3)
    expected = set.intersection(expected1, expected2, expected3)
    if computed != expected:
        return False
    
    return True


###############################################################################
def _test_pure_logic_expressions(job_id, cf, data, mongo_obj):

    # rename some precomputed sets
    tachy  = data['hasTachycardia']
    shock  = data['hasShock']
    rigors = data['hasRigors']
    dysp   = data['hasDyspnea']
    nau    = data['hasNausea']
    vom    = data['hasVomiting']
    temp   = data['Temperature']
    lesion = data['Lesion']
    
    expr = 'hasTachycardia NOT hasShock'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = tachy - shock
    if computed != expected:
        return False

    expr = '(hasTachycardia AND hasDyspnea) NOT hasRigors'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = (tachy & dysp) - rigors
    if computed != expected:
        return False

    expr = '((hasShock) AND (hasDyspnea))'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = shock & dysp
    if computed != expected:
        return False

    expr = '((hasTachycardia) AND (hasRigors OR hasDyspnea OR hasNausea))'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    set1 = data['hasRigors'] | data['hasDyspnea'] | data['hasNausea']
    expected = tachy & (rigors | dysp | nau)
    if computed != expected:
        return False

    expr = '((hasTachycardia)AND(hasRigorsORhasDyspneaORhasNausea))'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    if computed != expected:
        return False

    expr = 'hasTachycardia NOT (hasRigors OR hasDyspnea)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = tachy - (rigors | dysp)
    if computed != expected:
        return False

    expr = 'hasTachycardia NOT (hasRigors OR hasDyspnea OR hasNausea)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = tachy - (rigors | dysp | nau)
    if computed != expected:
        return False

    expr = 'hasTachycardia NOT (hasRigors OR hasDyspnea OR hasNausea OR ' \
        'hasVomiting)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = tachy - (rigors | dysp | nau | vom)
    if computed != expected:
        return False

    expr = 'hasTachycardia NOT (hasRigors OR hasDyspnea OR hasNausea OR ' \
        'hasVomiting OR hasShock)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = tachy - (rigors | dysp | nau | vom | shock)
    if computed != expected:
        return False

    expr = 'hasTachycardia NOT (hasRigors OR hasDyspnea OR hasNausea OR ' \
        'hasVomiting OR hasShock OR Temperature)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = tachy - (rigors | dysp | nau | vom | shock | temp)
    if computed != expected:
        return False
    
    expr = 'hasTachycardia NOT (hasRigors OR hasDyspnea OR hasNausea OR ' \
        'hasVomiting OR hasShock OR Temperature OR Lesion)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = tachy - (rigors | dysp | nau | vom | shock | temp | lesion)
    if computed != expected:
        return False

    expr = 'hasTachycardia NOT (hasRigors AND hasDyspnea)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = tachy - (rigors & dysp)
    if computed != expected:
        return False

    expr = 'hasRigors AND hasTachycardia AND hasDyspnea'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = rigors & tachy & dysp
    if computed != expected:
        return False

    expr = 'hasRigors AND hasDyspnea AND hasTachycardia'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = rigors & dysp & tachy
    if computed != expected:
        return False
    
    expr = 'hasRigors OR hasTachycardia AND hasDyspnea'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = rigors | tachy & dysp
    if computed != expected:
        return False
    
    expr = '(hasRigors OR hasDyspnea) AND hasTachycardia'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = (rigors | dysp) & tachy
    if computed != expected:
        return False
    
    expr = 'hasRigors AND (hasTachycardia AND hasNausea)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = rigors & (tachy & nau)
    if computed != expected:
        return False
    
    expr = '(hasShock OR hasDyspnea) AND (hasTachycardia OR hasNausea)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = (shock | dysp) & (tachy | nau)
    if computed != expected:
        return False
    
    expr = '(hasShock OR hasRigors) NOT (hasTachycardia OR hasNausea)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = (shock | rigors) - (tachy | nau)
    if computed != expected:
        return False
    
    expr = 'Temperature AND (hasDyspnea OR hasTachycardia)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = temp & (dysp | tachy)
    if computed != expected:
        return False
    
    expr = 'Lesion AND (hasDyspnea OR hasTachycardia)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = lesion & (dysp | tachy)
    if computed != expected:
        return False
    
    return True


###############################################################################
def _test_mixed_math_and_logic_expressions(job_id, cf, data, mongo_obj):

    # rename some precomputed sets
    tachy  = data['hasTachycardia']
    shock  = data['hasShock']
    rigors = data['hasRigors']
    dysp   = data['hasDyspnea']
    nau    = data['hasNausea']
    vom    = data['hasVomiting']
    temp   = data['Temperature']
    lesion = data['Lesion']

    expr = 'hasNausea AND Temperature.value >= 100.4'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            '$and':
            [
                {'nlpql_feature':'Temperature'},
                {'value':{'$gte':100.4}}
            ]
        })
    set1 = _to_context_set(cf, docs)
    expected = nau & set1
    if computed != expected:
        return False

    expr = 'Lesion.dimension_X < 10 AND hasRigors'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            '$and':
            [
                {'nlpql_feature':'Lesion'},
                {'dimension_X':{'$lt':10}}
            ]
        })
    set1 = _to_context_set(cf, docs)
    expected = set1 & rigors
    if computed != expected:
        return False

    expr = 'Lesion.dimension_X < 10 OR hasRigors'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            '$and':
            [
                {'nlpql_feature':'Lesion'},
                {'dimension_X':{'$lt':10}}
            ]
        })
    set1 = _to_context_set(cf, docs)
    expected = set1 | rigors
    if computed != expected:
        return False

    expr = '(hasRigors OR hasTachycardia OR hasNausea OR hasVomiting OR ' \
        'hasShock) AND '                                                  \
        '(Temperature.value >= 100.4 AND Temperature.value < 102)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            '$and':
            [
                {'nlpql_feature':'Temperature'},
                {'value':{'$gte':100.4}},
                {'value':{'$lt':102}}
            ]
        })
    set1 = _to_context_set(cf, docs)
    expected = (rigors | tachy | nau | vom | shock) & set1
    if computed != expected:
        return False

    expr = 'Lesion.dimension_X > 10 AND Lesion.dimension_X < 30 OR ' \
        '(hasRigors OR hasTachycardia AND hasDyspnea)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            '$and':
            [
                {'nlpql_feature':'Lesion'},
                {'dimension_X':{'$gt':10}},
                {'dimension_X':{'$lt':30}}
            ]
        })
    set1 = _to_context_set(cf, docs)
    expected = set1 | (rigors | tachy & dysp)
    if computed != expected:
        return False

    expr = 'Lesion.dimension_X > 10 AND Lesion.dimension_X < 30 OR ' \
        'hasRigors OR hasTachycardia OR hasDyspnea'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = set1 | rigors | tachy | dysp
    if computed != expected:
        return False

    expr = '(Lesion.dimension_X > 10 AND Lesion.dimension_X < 30) NOT ' \
        '(hasRigors OR hasTachycardia OR hasDyspnea)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = set1 - (rigors | tachy | dysp)
    if computed != expected:
        return False

    expr = '(Temperature.value >= 100.4) AND ' \
        'hasDyspnea AND hasNausea AND hasVomiting'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            '$and':
            [
                {'nlpql_feature':'Temperature'},
                {'value':{'$gt':100.4}}
            ]
        })
    set1 = _to_context_set(cf, docs)
    expected = set1 & dysp & nau & vom
    if computed != expected:
        return False

    expr = 'hasRigors OR (hasTachycardia AND hasDyspnea) AND ' \
        'Temperature.value >= 100.4'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = rigors | (tachy & dysp) & set1
    if computed != expected:
        return False

    expr = '(hasRigors OR hasTachycardia OR hasDyspnea OR hasNausea) AND ' \
        'Temperature.value >= 100.4'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = (rigors | tachy | dysp | nau) & set1
    if computed != expected:
        return False

    expr = 'Lesion.dimension_X < 10 OR hasRigors AND Lesion.dimension_X > 30'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs1 = mongo_obj.find(
        {
            '$and':
            [
                {'nlpql_feature':'Lesion'},
                {'dimension_X':{'$lt':10}}
            ]
        })
    set1 = _to_context_set(cf, docs1)
    docs2 = mongo_obj.find(
        {
            '$and':
            [
                {'nlpql_feature':'Lesion'},
                {'dimension_X':{'$gt':30}}
            ]
        })
    set2 = _to_context_set(cf, docs2)
    expected = set1 | rigors & set2
    if computed != expected:
        return False

    # redundant math expressions

    expr = 'Lesion.dimension_X > 50'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            '$and':
            [
                {'nlpql_feature':'Lesion'},
                {'dimension_X':{'$gt':50}}
            ]
        })
    set1 = _to_context_set(cf, docs)
    if computed != set1:
        return False

    expr = 'Lesion.dimension_X > 30 AND Lesion.dimension_X > 50'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    if computed != set1:
        return False

    expr = 'Lesion.dimension_X > 12 AND Lesion.dimension_X > 30 AND ' \
        'Lesion.dimension_X > 50'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    if computed != set1:
        return False

    expr = '(Lesion.dimension_X > 50) OR (hasNausea AND hasDyspnea)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected = set1 | (nau & dysp)
    if computed != expected:
        return False

    expr = '(Lesion.dimension_X > 30 AND Lesion.dimension_X > 50) OR ' \
        '(hasNausea AND hasDyspnea)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    if computed != expected:
        return False

    expr = '(Lesion.dimension_X > 12 AND Lesion.dimension_X > 50) OR ' \
        '(hasNausea AND hasDyspnea)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    if computed != expected:
        return False

    expr = '(Lesion.dimension_X > 12 AND Lesion.dimension_X > 30 AND ' \
        'Lesion.dimension_X > 50) OR (hasNausea AND hasDyspnea)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    if computed != expected:
        return False
    
    return True


###############################################################################
def _test_not_with_positive_logic(job_id, cf, data, mongo_obj):

    # rename some precomputed sets
    tachy  = data['hasTachycardia']
    rigors = data['hasRigors']
    dysp   = data['hasDyspnea']

    # expression without using NOT
    expr = '(hasRigors OR hasDyspnea OR hasTachycardia) AND ' \
        '(Temperature.value < 100.4)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            '$and':
            [
                {'nlpql_feature':'Temperature'},
                {'value':{'$lt':100.4}}
            ]
        })
    set1 = _to_context_set(cf, docs)
    expected = (rigors | dysp | tachy) & set1
    if computed != expected:
        return False

    # equivalent using NOT
    expr = '(hasRigors OR hasDyspnea OR hasTachycardia) NOT ' \
        '(Temperature.value >= 100.4)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    if computed != expected:
        return False

    # expression 2 without using NOT
    expr = '(hasRigors OR hasDyspnea) AND ' \
        '(Temperature.value >= 99.5 AND Temperature.value <= 101.5)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    docs = mongo_obj.find(
        {
            '$and':
            [
                {'nlpql_feature':'Temperature'},
                {'value':{'$gte':99.5}},
                {'value':{'$lte':101.5}}
            ]
        })
    set1 = _to_context_set(cf, docs)
    expected = (rigors | dysp) & set1
    if computed != expected:
        return False

    # equivalent using NOT
    expr = '(hasRigors OR hasDyspnea) NOT ' \
        '(Temperature.value < 99.5 OR Temperature.value > 101.5)'
    computed = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    if computed != expected:
        return False
    
    return True


###############################################################################
def _test_set_relations_1(job_id, cf, data, mongo_obj):

    # rename some precomputed sets
    rigors = data['hasRigors']
    dysp   = data['hasDyspnea']
    
    expr = 'hasRigors OR hasDyspnea'
    computed_or = _run_selftest_expression(job_id, cf, expr, mongo_obj)
    expected_or = rigors | dysp
    if computed_or != expected_or:
        return False

    expr2 = 'hasRigors'
    computed_rigors = _run_selftest_expression(job_id, cf, expr2, mongo_obj)
    if computed_rigors != rigors:
        return False

    expr3 = 'hasDyspnea'
    computed_dysp = _run_selftest_expression(job_id, cf, expr3, mongo_obj)
    if computed_dysp != dysp:
        return False

    expr4 = 'hasRigors AND hasDyspnea'
    computed_and = _run_selftest_expression(job_id, cf, expr4, mongo_obj)
    expected_and = rigors & dysp
    if computed_and != expected_and:
        return False
    
    expr5 = '(hasRigors OR hasDyspnea) NOT (hasRigors AND hasDyspnea)'
    computed_5 = _run_selftest_expression(job_id, cf, expr5, mongo_obj)
    expected_5 = (rigors | dysp) - (rigors & dysp)
    if computed_5 != expected_5:
        return False

    # use vertical bars to denote set cardinality
    
    # first check:
    #     |rigors OR dysp| == |rigors| + |dysp| - |rigors & dysp|
    lhs = len(computed_or)
    rhs = len(computed_rigors) + len(computed_dysp) - len(computed_and)
    if lhs != rhs:
        return False
    if lhs != len(expected_or):
        return False

    # second check:
    #    |rigors OR dysp| == |(rigors | dysp) - (rigors & dysp)| + |rigors & dysp|
    lhs = len(computed_or)
    rhs = len(computed_5) + len(computed_and)
    if lhs != rhs:
        return False
    if lhs != len(expected_or):
        return False
    
    return True


###############################################################################
def run_self_tests(job_id,
                   context_var,
                   mongohost,
                   mongoport,
                   debug=False):
    """
    Run test expressions and verify with independent MongoDB queries.
    """

    if debug:
        _enable_debug()
        expr_eval.enable_debug()
    
    DB_NAME         = 'claritynlp_eval_test'
    COLLECTION_NAME = 'eval_test_data'
    TEST_FILE_NAME  = 'expr_test_data.json'

    # use mongoimport to load JSON file containing test data
    command = []
    command.append('mongoimport')
    command.append('--host')
    command.append(str(mongohost))
    command.append('--port')
    command.append(str(mongoport))
    command.append('--db')
    command.append(DB_NAME)
    command.append('--collection')
    command.append(COLLECTION_NAME)
    command.append('--file')
    command.append(TEST_FILE_NAME)
    command.append('--stopOnError')
    command.append('--drop')

    cp = subprocess.run(command,
                        stdout=subprocess.PIPE,
                        universal_newlines=True)
    if 0 != len(cp.stdout):
        # an error occurred
        print(cp.stdout)
        return False
    
    # must either be a patient or document context
    context_var = context_var.lower()
    assert 'patient' == context_var or 'document' == context_var

    # determine context field from the context varialbe
    if 'patient' == context_var:
        cf = 'subject'
    else:
        cf = 'report_id'

    # connect to this collection
    mongo_client_obj = MongoClient(mongohost, mongoport)
    mongo_db_obj = mongo_client_obj[DB_NAME]
    mongo_obj = mongo_db_obj[COLLECTION_NAME]

    # direct Mongo query results; will perform set ops with python on these
    data = {}
    data['Temperature']    = _get_feature_set(mongo_obj, cf, 'Temperature')
    data['Lesion']         = _get_feature_set(mongo_obj, cf, 'Lesion')
    data['hasRigors']      = _get_feature_set(mongo_obj, cf, 'hasRigors')
    data['hasDyspnea']     = _get_feature_set(mongo_obj, cf, 'hasDyspnea')
    data['hasNausea']      = _get_feature_set(mongo_obj, cf, 'hasNausea')
    data['hasVomiting']    = _get_feature_set(mongo_obj, cf, 'hasVomiting')
    data['hasShock']       = _get_feature_set(mongo_obj, cf, 'hasShock')
    data['hasTachycardia'] = _get_feature_set(mongo_obj, cf, 'hasTachycardia')

    
    if not _test_basic_expressions(job_id, cf, data, mongo_obj):
        return False
    if not _test_pure_math_expressions(job_id, cf, mongo_obj):
        return False
    if not _test_math_with_multiple_features(job_id, cf, mongo_obj):
        return False
    if not _test_pure_logic_expressions(job_id, cf, data, mongo_obj):
        return False
    if not _test_mixed_math_and_logic_expressions(job_id, cf, data, mongo_obj):
        return False
    if not _test_not_with_positive_logic(job_id, cf, data, mongo_obj):
        return False
    if not _test_set_relations_1(job_id, cf, data, mongo_obj):
        return False
    
    # drop the collection and database
    mongo_obj.drop()
    mongo_client_obj.drop_database(DB_NAME)
    
    return True

    
###############################################################################
def _run_tests(job_id,
               final_nlpql_feature,
               command_line_expression,
               context_var,
               mongo_collection_obj,
               num,
               is_final,
               name_list=None,
               debug=False):

    EXPRESSIONS = [

        # counts are for job 11222
        
        # all temperature measurements
        # 'Temperature', # 945 results

        # all lesion measurements
        # 'Lesion',      # 2425 results

        # all instances of a temp measurement AND a lesion measurement
        # 'Temperature AND Lesion', # 17 results

        # all instances of the given symptoms
        # 'hasTachycardia', # 1996 results, 757 groups
        # 'hasRigors',      # 683 results, 286 groups
        # 'hasShock',       # 2117 results, 521 groups
        # 'hasDyspnea',     # 3277 results, 783 groups
        # 'hasNausea',      # 2261 results, 753 groups
        # 'hasVomiting',    # 2303 results, 679 groups

        # all instances of a temp measurement and another symptom
        # 'Temperature AND hasTachycardia', # 55 results, 13 groups
        # 'Temperature AND hasRigors',      # 11 results, 5 groups
        # 'Temperature AND hasShock',       # 50 results, 11 groups
        # 'Temperature AND hasDyspnea',     # 64 results, 11 groups
        # 'Temperature AND hasNausea',      # 91 results, 17 groups
        # 'Temperature AND hasVomiting',    # 74 results, 13 groups

        # all instances of a lesion measurement and another symptom
        # 'Lesion AND hasTachycardia', # 131 results, 24 groups
        # 'Lesion AND hasRigors',      # 50 results, 11 groups
        # 'Lesion AND hasShock',       # 43 results, 10 groups
        # 'Lesion AND hasDyspnea',     # 103 results, 21 groups
        # 'Lesion AND hasNausea',      # 136 results, 30 groups
        # 'Lesion AND hasVomiting',    # 150 results, 26 groups

        # pure math expressions
        # 'Temperature.value >= 100.4',    # 488 results
        # 'Temperature.value >= 1.004e2',  # 488 results
        # '100.4 <= Temperature.value',    # 488 results
        # '(Temperature.value >= (100.4))',  # 488 results
        # 'Temperature.value == 100.4',    # 14 results
        # 'Temperature.value + 3 ^ 2 < 109',      # temp < 100,     374 results
        # 'Temperature.value ^ 3 + 2 < 941194',   # temp < 98,      118 results
        # 'Temperature.value % 3 ^ 2 == 2',       # temp == 101,    68 results
        # 'Temperature.value * 4 ^ 2 >= 1616',    # temp >= 101,    417 results
        # 'Temperature.value / 98.6 ^ 2 < 0.01',  # temp < 97.2196, 66 results
        # '(Temperature.value / 98.6)^2 < 1.02',  # temp < 99.581,  325 results
        # '0 == Temperature.value % 20',          # temp == 100,    40 results
        # '(Lesion.dimension_X <= 5) OR (Lesion.dimension_X >= 45)',           # 746 results
        # 'Lesion.dimension_X > 15 AND Lesion.dimension_X < 30',               # 528 results
        # '((Lesion.dimension_X) > (15)) AND (((Lesion.dimension_X) < (30)))', # 528 results

        # math involving multiple NLPQL features
        # 'Lesion.dimension_X > 15 AND Lesion.dimension_X < 30 OR (Temperature.value >= 100.4)', # 1016 results
        # '(Lesion.dimension_X > 15 AND Lesion.dimension_X < 30) AND Temperature.value > 100.4', # 2 results
        # 'Lesion.dimension_X > 15 AND Lesion.dimension_X < 30 AND Temperature.value > 100.4', # 2 results

        # need to remove duplicate results for the same patient?? TBD
        # '(Temperature.value >= 102) AND (Lesion.dimension_X <= 5)',  # 4 results
        # '(Temperature.value >= 102) AND (Lesion.dimension_X <= 5) AND (Temperature.value >= 103)', # 2 results

        # pure logic
        # 'hasTachycardia AND hasShock',                  # 191 results, 25 groups
        # 'hasTachycardia OR hasShock',                   # 4113 results, 1253 groups
        # 'hasTachycardia NOT hasShock',                  # 1891 results, 732 groups
        # '(hasTachycardia AND hasDyspnea) NOT hasRigors' # 229 results, 46 groups
        # 'hasTachycardia AND hasDyspnea',                # 240 results, 49 groups
        # '((hasShock) AND (hasDyspnea))',                # 155 results, 22 groups
        # '((hasTachycardia) AND (hasRigors OR hasDyspnea OR hasNausea))', # 546 results, 112 groups
        # '((hasTachycardia)AND(hasRigorsORhasDyspneaORhasNausea))',       # 546 results, 112 groups
        # 'hasTachycardia NOT (hasRigors OR hasDyspnea)',   # 1800 results, 683 groups
        # 'hasTachycardia NOT (hasRigors OR hasDyspnea OR hasNausea)',     # 1702 results, 645 groups
        # 'hasTachycardia NOT (hasRigors OR hasDyspnea OR hasNausea OR hasVomiting)', # 1622 results, 619 groups
        # 'hasTachycardia NOT (hasRigors OR hasDyspnea OR hasNausea OR hasVomiting OR hasShock)', # 1529r, 599 g
        # 'hasTachycardia NOT (hasRigors OR hasDyspnea OR hasNausea OR hasVomiting OR hasShock ' \
        # 'OR Temperature)', # 1491 results, 589 groups
        # 'hasTachycardia NOT (hasRigors OR hasDyspnea OR hasNausea OR hasVomiting OR hasShock ' \
        # 'OR Temperature OR Lesion)', # 1448 results, 569 groups

        # 'hasTachycardia NOT (hasRigors AND hasDyspnea)',  # 1987 results, 754 groups
        # 'hasRigors AND hasTachycardia AND hasDyspnea',    # 11 results, 3 groups
        # 'hasRigors AND hasDyspnea AND hasTachycardia',    # 11 results, 3 groups
        # 'hasRigors OR hasTachycardia AND hasDyspnea',     # 923 results, 332 groups
        # '(hasRigors OR hasDyspnea) AND hasTachycardia',   # 340 results, 74 groups
        # 'hasRigors AND (hasTachycardia AND hasNausea)',   # 22 results, 5 groups
        # '(hasShock OR hasDyspnea) AND (hasTachycardia OR hasNausea)', # 743 results, 129 groups
        # '(hasShock OR hasRigors) NOT (hasTachycardia OR hasNausea)', # 2468 results, 705 groups
        
        # 'Temperature AND (hasDyspnea OR hasTachycardia)',  # 106 results, 22 groups
        # 'Lesion AND (hasDyspnea OR hasTachycardia)',       # 234 results, 45 groups

        # mixed math and logic 
        # 'hasNausea AND Temperature.value >= 100.4', # 73 results, 16 groups
        # 'Lesion AND hasRigors',                     # 50 results, 11 groups
        # 'Lesion.dimension_X < 10 AND hasRigors',    # 19 results, 7 groups
        # 'Lesion.dimension_X < 10',                  # 841 results
        # 'Lesion.dimension_X < 10 OR hasRigors',     # 1524 results, 633 groups
        # '(hasRigors OR hasTachycardia OR hasNausea OR hasVomiting OR hasShock) AND ' \
        # '(Temperature.value >= 100.4)',             # 180 results, 38 groups

        # 1808 results, 702 groups
        # 'Lesion.dimension_X > 10 AND Lesion.dimension_X < 30 OR (hasRigors OR hasTachycardia AND hasDyspnea)',

        # 6841 results, 2072 groups
        # 'Lesion.dimension_X > 10 AND Lesion.dimension_X < 30 OR hasRigors OR hasTachycardia OR hasDyspnea',

        # 797 results, 341 groups
        # '(Lesion.dimension_X > 10 AND Lesion.dimension_X < 30) NOT (hasRigors OR hasTachycardia OR hasDyspnea)',

        # 'Temperature AND hasDyspnea AND hasNausea AND hasVomiting', # 22 results, 2 groups
        # '(Temperature.value > 100.4) AND hasDyspnea AND hasNausea AND hasVomiting', # 20 results, 2 groups
        # 692 results, 287 groups
        # 'hasRigors OR (hasTachycardia AND hasDyspnea) AND Temperature.value >= 100.4',
        # 155 results, 33 groups
        # '(hasRigors OR hasTachycardia OR hasDyspnea OR hasNausea) AND Temperature.value >= 100.4',
        # 'Lesion.dimension_X < 10 OR hasRigors AND Lesion.dimension_X > 30', # 851 results, 356 groups

        # redundant math expressions
        # 'Lesion.dimension_X > 50',  # 246 results
        # 'Lesion.dimension_X > 30 AND Lesion.dimension_X > 50',  # 246 results
        # 'Lesion.dimension_X > 12 AND Lesion.dimension_X > 30 AND Lesion.dimension_X > 50', # 246 results
        # '(Lesion.dimension_X > 50) OR (hasNausea AND hasDyspnea)', # 518 results, 195 groups
        # 518 results, 195 groups
        # '(Lesion.dimension_X > 30 AND Lesion.dimension_X > 50) OR (hasNausea AND hasDyspnea)',
        # 518 results, 195 groups
        # '(Lesion.dimension_X > 12 AND Lesion.dimension_X > 50) OR (hasNausea AND hasDyspnea)',
        # 518 results, 195 groups
        # '(Lesion.dimension_X > 12 AND Lesion.dimension_X > 30 AND Lesion.dimension_X > 50) OR '
        # '(hasNausea AND hasDyspnea)',
        
        # 'Lesion.dimension_X > 10 AND Lesion.dimension_X < 30', # 885 results
        # 'Lesion.dimension_X > 5 AND Lesion.dimension_X > 10 AND ' \
        # 'Lesion.dimension_X < 40 AND Lesion.dimension_X < 30',   # 885 results
        # '(Lesion.dimension_X > 8 AND Lesion.dimension_X > 5 AND Lesion.dimension_X > 10) AND '
        # '(Lesion.dimension_X < 40 AND Lesion.dimension_X < 30 AND Lesion.dimension_X < 45)', # 885 results

        # checking NOT with positive logic
        
        #  of this group of four, the final two expressions are identical
        # '(hasRigors OR hasDyspnea OR hasTachycardia) AND Temperature', # 117 results, 25 groups
        # '(hasRigors OR hasDyspnea OR hasTachycardia) AND (Temperature.value >= 100.4)', # 82 results, 20 groups
        # '(hasRigors OR hasDyspnea OR hasTachycardia) AND (Temperature.value < 100.4)',  # 53 results, 10 groups
        # '(hasRigors OR hasDyspnea OR hasTachycardia) NOT (Temperature.value >= 100.4)', # 53 results, 10 groups

        # final two in this group should be identical
        # '(hasRigors OR hasDyspnea) AND Temperature', # 75 results, 14 groups
        # '(hasRigors OR hasDyspnea) AND (Temperature.value >= 99.5 AND Temperature.value <= 101.5)', # 34r, 7g
        # '(hasRigors OR hasDyspnea) NOT (Temperature.value < 99.5  OR  Temperature.value > 101.5)', # 34r, 7g

        
        # Checking the behavior of NOT with set theory relations:
        #
        #     Let P == probability, or for our purposes here, the unique element count in a given set.
        #
        #     'Groups' refers to grouping the results on the value of the context variable, which
        #     is either the document ID or patient ID. So the group count is the number of distinct
        #     values of the context variable (the unique docs or unique patients).
        #
        #     'Results' refers to the rows of output in the CSV file. This will vary with the
        #     form of the expression and will contain some amount of redundancy. The redundancy
        #     is required to flatten the data into a row-based spreadsheet format. What matters
        #     is the UNIQUE data per patient or document, which is why groups are more important.
        #
        #     The set relations of interest are (think of Venn diagrams):
        #
        #     P(A OR B) == P(A) + P(B) - P(A AND B)
        # 
        #     P(A OR B OR C) == P(A) + P(B) + P(C) - (P(A AND B) + P(A AND C) + P(B AND C)) + P(A AND B AND C)
        #
        # If 'Groups' denotes the number of context variable groups in the expression evaluator result,
        # these relations should hold:
        #
        #     Groups[A OR B] == Groups[A] + Groups[B] - Groups[A AND B]
        #     Groups[A OR B] == Groups[(A OR B) NOT (A AND B)] + Groups[A AND B]
        #
        #     Groups[A OR B OR C] == Groups[A] + Groups[B] + Groups[C] -
        #                            ( Groups[A AND B] + Groups[A AND C] + Groups[B AND C] ) +
        #                            Groups[ A AND B AND C ]
        #
        #     For the second relation, we need to evaluate this expression:
        #
        #         (A OR B OR C) NOT ( (A AND B) OR (A AND C) OR (B AND ) ) OR (A AND B AND C)
        #
        #     This needs to be rearranged into this equivalent form, to make sure the NOT applies to ALL the
        #     available docs, and to prevent dependencies on the evaluation order:
        #
        #         ((A OR B OR C) OR (A AND B AND C)) NOT ( (A AND B) OR (A AND C) OR (B AND C) )
        #
        #     The subexpression prior to the NOT is an OR of two terms. There could be duplicates between these two
        #     sets, so the duplicates need to be subtracted out as well. A direct evaluation of this expression will
        #     give the minimal result set PLUS any duplicates between (A OR B OR C) and (A AND B AND C).
        #
        #     Thus the relation for the second check becomes:
        #
        #     Groups[A OR B OR C] == Groups[ ((A OR B OR C) OR (A AND B AND C)) NOT ((A AND B) OR (A AND C) OR (B AND C) )] +
        #                            Groups[A AND B] + Groups[A AND C] + Groups[B AND C] -
        #                            Groups[A AND B AND C] -
        #                            Groups[ (A OR B OR C) AND (A AND B AND C) ]
        #
        #     In the expressions below, anything contained in single quotes is sent to the evaluator.
        #

        # 1. hasRigors OR hasDyspnea
        # ----------------------------
        # '(hasRigors OR hasDyspnea)', # 3960 results, 1048 groups direct evaluation
        # 'hasRigors',                 # 683 results, 286 groups
        # 'hasDyspnea',                # 3277 results, 783 groups
        # 'hasRigors AND hasDyspnea',  # 89 results, 21 groups
        # '(hasRigors OR hasDyspnea) NOT (hasRigors AND hasDyspnea)', # 3825 results, 1027 groups
        # group check 1:  Groups[hasRigors] + Groups[hasDyspnea] - Groups[hasRigors AND hasDyspnea]
        #                   286 + 783 - 21 = 1048, identical to Groups[hasRigors OR hasDyspnea]
        # group check 2:  Groups[(hasRigors OR hasDyspnea) NOT (hasRigors AND hasDyspnea)] + Groups[hasRigors AND hasDyspnea]
        #                   1027 + 21 = 1048 groups, identical to Groups[hasRigors OR hasDyspnea]

        # 2. hasTachycardia OR hasShock
        # -------------------------------
        # 'hasTachycardia OR hasShock',  # 4113 results, 1253 groups direct evaluation
        # 'hasTachycardia',              # 1996 results, 757 groups
        # 'hasShock',                    # 2117 results, 521 groups
        # 'hasTachycardia AND hasShock', # 191 results, 25 groups
        # '(hasTachycardia OR hasShock) NOT (hasTachycardia AND hasShock)', # 3867 results, 1228 groups
        # group check 1: Groups[hasTachycardia] + Groups[hasShock] - Groups[hasTachycardia AND hasShock]
        #                  757 + 521 - 25 = 1253, identical to Groups[hasTachycardia OR hasShock]
        # group check 2: Groups[(hasTachycardia OR hasShock) NOT (hasTachycardia AND hasShock)] + Groups[hasTachycardia AND hasShock]
        #                 1228 + 25 = 1253, identical to Groups[hasTachycardia OR hasShock]

        # 3. hasShock OR hasDyspnea OR hasTachycardia
        # -------------------------------------------
        # 'hasShock OR hasDyspnea OR hasTachycardia',     # 7390 results, 1967 groups
        # 'hasShock',                                     # 2117 results, 521 groups
        # 'hasDyspnea',                                   # 3277 results, 783 groups
        # 'hasTachycardia',                               # 1996 results, 757 groups
        # 'hasShock AND hasDyspnea',                      # 155 results, 22 groups
        # 'hasShock AND hasTachycardia',                  # 191 results, 25 groups
        # 'hasDyspnea AND hasTachycardia',                # 240 results, 49 groups
        # 'hasShock AND hasDyspnea AND hasTachycardia',   # 11 results, 2 groups
        # '((hasShock OR hasDyspnea OR hasTachycardia) OR (hasShock AND hasDyspnea AND hasTachycardia)) NOT ( (hasShock AND hasDyspnea) OR (hasShock AND hasTachycardia) OR (hasDyspnea AND hasTachycardia) )' # 6607 results, 1875 groups
        # '((hasShock OR hasDyspnea OR hasTachycardia) AND (hasShock AND hasDyspnea AND hasTachycardia))', # 20 results, 2 groups
        # group check 1: Groups[hasShock] + Groups[hasDyspnea] + Groups[hasTachycardia] -
        #                (Groups[hasShock AND hasDyspnea] + Groups[hasShock AND hasTachycardia] + Groups[hasDyspnea AND hasTachycardia]) +
        #                Groups[hasShock + hasDyspnea + hasTachycardia]
        #                521 + 783 + 757 - (22 + 25 + 49) + 2 = 1967 groups, identical to Groups[hasShock OR hasDyspnea OR hasTachycardia]
        # group check 2: Groups[((shock OR dysp OR tachy) OR (shock AND dysp AND tachy)) NOT ( (shock AND dysp) OR (shock AND tachy) OR (dysp AND tachy)] +
        #                Groups[shock and dysp] + Groups[shock and tachy] + Groups[dysp and tachy] -
        #                Groups[shock AND dysp AND tachy] -
        #                Groups[(shock OR dysp OR tachy) AND (shock AND dysp AND tachy)]
        #                1875 + 22 + 25 + 49 - 2 - 2 = 1967 groups, identical to Groups[hasShock OR hasDyspnea OR hasTachycardia]

        # 4. hasTachycardia OR hasShock OR hasRigors
        # ------------------------------------------
        # 'hasTachycardia OR hasShock OR hasRigors',   # 4796 results, 1502 groups
        # 'hasTachycardia',                            # 1996 results, 757 groups
        # 'hasShock',                                  # 2117 results, 521 groups
        # 'hasRigors',                                 # 683 results, 286 groups
        # 'hasTachycardia AND hasShock',               # 191 results, 25 groups
        # 'hasTachycardia AND hasRigors',              # 104 results, 28 groups
        # 'hasShock AND hasRigors',                    # 52 results, 11 groups
        # 'hasTachycardia AND hasShock AND hasRigors', # 11 results, 2 groups
        # '((hasTachycardia OR hasShock OR hasRigors) OR (hasTachycardia AND hasShock AND hasRigors)) NOT ( (hasTachycardia AND hasShock) OR (hasTachycardia AND hasRigors) OR (hasShock AND hasRigors) )', # 4338 results, 1442 groups
        # '((hasTachycardia OR hasShock OR hasRigors) AND (hasTachycardia AND hasShock AND hasRigors))', # 22 results, 2 groups
        # group check 1: 757 + 521 + 286 - (25 + 28 + 11) + 2 = 1502 groups, identical to direct eval
        # group check 2: 1442 + 25 + 28 + 11 - 2 - 2 = 1502 groups, identical to direct eval

        #     Groups[A OR B] == Groups[A] + Groups[B] - Groups[A AND B]
        #     Groups[A OR B] == Groups[(A OR B) NOT (A AND B)] + Groups[A AND B]
        
        # the same, but group as (hasTachycardia OR hasShock) OR hasRigors and use two-component formula
        # ----------------------------------------------------------------------------------------------
        # 'hasTachycardia OR hasShock',                 # 4113 results, 1253 groups
        # 'hasRigors',                                  # 683 results, 286 groups
        # '(hasTachycardia OR hasShock) AND hasRigors', # 152 results, 37 groups
        # '((hasTachycardia OR hasShock) OR hasRigors) NOT ( (hasTachycardia OR hasShock) AND hasRigors)', # 4568 results, 1465 groups
        # group check 1: Groups[hasTachycardia OR hasShock] + Groups[hasRigors] - Groups[(hasTachycardia OR hasShock) AND hasRigors]
        #                  1253 + 286 - 37 == 1502 groups, identical to Groups[hasTachycardia OR hasShock OR hasRigors]
        # group check 2: Groups[((hasTachycardia OR hasShock) OR hasRigors) NOT ( (hasTachycardia OR hasShock) AND hasRigors)] + Groups[(hasTachycardia OR hasShock) AND hasRigors]
        #                  1465 + 37 = 1502 groups, identical to Groups[hasTachycardia OR hasShock OR hasRigors]

        # the same, but group as hasTachycardia OR (hasShock OR hasRigors) and use two-component formula
        # ----------------------------------------------------------------------------------------------
        # 'hasTachycardia',                             # 1996 results, 757 groups
        # 'hasShock OR hasRigors',                      # 2800 results, 796 groups
        # 'hasTachycardia AND (hasShock OR hasRigors)', # 292 results, 51 groups
        # '(hasTachycardia OR (hasShock OR hasRigors)) NOT ( hasTachycardia AND (hasShock OR hasRigors) )', # 4402 results, 1451 groups
        # group check 1: Groups[hasTachycardia] + Groups[hasShock or hasRigors] - Groups[hasTachycardia AND (hasShock OR hasRigors)]
        #                  757 + 796 - 51 = 1502 groups, identical to Groups[hasTachycardia OR hasShock OR hasRigors]
        # group check 2: Groups[(hasTachycardia OR (hasShock OR hasRigors)) NOT ( hasTachycardia AND (hasShock OR hasRigors) )] + Groups[hasTachycardia AND (hasShock OR hasRigors)]
        #                  1451 + 51 = 1502 groups, identical to Groups[hasTachycardia OR hasShock OR hasRigors]

        # 5. hasRigors OR hasDyspnea OR (Lesion.dimension_X >= 10 AND Lesion.dimension_Y < 10), mixed logic and math
        # ----------------------------------------------------------------------------------------------------------
        # 'hasRigors OR hasDyspnea OR (Lesion.dimension_X >= 10 AND Lesion.dimension_Y < 10)',   # 4028 results, 1104 groups
        # 'hasRigors',                                                                           # 683 results, 286 groups
        # 'hasDyspnea',                                                                          # 3277 results, 783 groups
        # '(Lesion.dimension_X >= 10 AND Lesion.dimension_Y < 10)',                              # 67 results, 57 groups
        # 'hasRigors AND hasDyspnea',                                                            # 89 results, 21 groups
        # 'hasRigors AND (Lesion.dimension_X >= 10 AND Lesion.dimension_Y < 10)',                # 0 results, 0 groups
        # 'hasDyspnea AND (Lesion.dimension_X >= 10 AND Lesion.dimension_Y < 10)',               # 5 results, 1 group
        # 'hasRigors AND hasDyspnea AND (Lesion.dimension_X >= 10 AND Lesion.dimension_Y < 10)', # 0 results, 0 groups
        # # 3886 results, 1082 groups
        # '((hasRigors OR hasDyspnea OR (Lesion.dimension_X >= 10 AND Lesion.dimension_Y < 10)) OR (hasRigors AND hasDyspnea AND (Lesion.dimension_X >= 10 AND Lesion.dimension_Y < 10))) ' \
        # 'NOT( (hasRigors AND hasDyspnea) OR (hasRigors AND (Lesion.dimension_X >= 10 AND Lesion.dimension_Y < 10)) OR (hasDyspnea AND (Lesion.dimension_X >= 10 AND Lesion.dimension_Y < 10)))'
        # 0 results, 0 groups
        # '((hasRigors OR hasDyspnea OR (Lesion.dimension_X >= 10 AND Lesion.dimension_Y < 10)) AND (hasRigors AND hasDyspnea AND (Lesion.dimension_X >= 10 AND Lesion.dimension_Y < 10)))',
        # group check 1: 286 + 783 + 57 - (21 + 0 + 1) + 0 = 1104 groups, expected result
        # group check 2: 1082 + 21 + 0 + 1 - 0 - 0 = 1104 groups, expected result
        
        # should generate a parser exception
        # 'This is junk and should cause a parser exception',

        # not a valid expression, since each math expression must produce a Boolean result
        # '(Temp.value/98.6) * (HR.value/60.0) * (BP.systolic/110) < 1.1',
    ]

    # must either be a patient or document context
    context_var = context_var.lower()
    assert 'patient' == context_var or 'document' == context_var

    if 'patient' == context_var:
        context_field = 'subject'
    else:
        context_field = 'report_id'

    # cleanup so that database only contains data generated by data_gen.nlpql
    # not from previous runs of this test code
    _delete_prev_results(job_id, mongo_collection_obj)

    if debug:
        _enable_debug()
        expr_eval.enable_debug()

    # get all defined names, helps resolve tokens if bad expression formatting
    the_name_list = _NAME_LIST
    if name_list is not None:
        the_name_list = name_list

    if command_line_expression is None:
        expressions = EXPRESSIONS
    else:
        expressions = [command_line_expression]
        
    counter = 1
    for e in expressions:

        print('[{0:3}]: "{1}"'.format(counter, e))

        parse_result = expr_eval.parse_expression(e, the_name_list)
        if 0 == len(parse_result):
            print('\n*** parse_expression failed ***\n')
            break
        
        # generate a list of ExpressionObject primitives
        expression_object_list = expr_eval.generate_expressions(final_nlpql_feature,
                                                                parse_result)
        if 0 == len(expression_object_list):
            print('\n*** generate_expressions failed ***\n')
            break
        
        # evaluate the ExpressionObjects in the list
        results = _evaluate_expressions(expression_object_list,
                                        mongo_collection_obj,
                                        job_id,
                                        context_field,
                                        is_final)

        banner_print(e)
        for expr_obj, output_docs in results:
            print()
            print('Subexpression text: {0}'.format(expr_obj.expr_text))
            print('Subexpression type: {0}'.format(expr_obj.expr_type))
            print('      Result count: {0}'.format(len(output_docs)))
            print('     NLPQL feature: {0}'.format(expr_obj.nlpql_feature))
            print('\nResults: ')

            n = len(output_docs)
            if 0 == n:
                print('\tNone.')
                continue

            if expr_eval.EXPR_TYPE_MATH == expr_obj.expr_type:
                for k in range(n):
                    if k < num or k > n-num:
                        doc = output_docs[k]
                        print('[{0:6}]: Document ...{1}, NLPQL feature {2}:'.
                              format(k, str(doc['_id'])[-6:],
                                     expr_obj.nlpql_feature))
                        
                        if 'history' in doc:
                            assert 1 == len(doc['history'])
                            data_field = doc['history'][0].data
                        else:
                            data_field = doc['value']

                        if 'subject' == context_field:
                            context_str = 'subject: {0:8}'.format(doc['subject'])
                        else:
                            context_str = 'report_id: {0:8}'.format(doc['report_id'])
                            
                        print('\t[{0:6}]: _id: {1} nlpql_feature: {2:16} ' \
                              '{3} data: {4}'.
                              format(k, doc['_id'], doc['nlpql_feature'],
                                     context_str, data_field))
                    elif k == num:
                        print('\t...')

            else:
                for k in range(n):
                    if k < num or k > n-num:
                        doc = output_docs[k]
                        print('[{0:6}]: Document ...{1}, NLPQL feature {2}:'.
                              format(k, str(doc['_id'])[-6:],
                                     expr_obj.nlpql_feature))

                        history = doc[expr_result.HISTORY_FIELD]
                        for tup in history:
                            if isinstance(tup.data, float):

                            # format data depending on whether float or string
                                data_string = '{0:<10}'.format(tup.data)
                            else:
                                data_string = '{0}'.format(tup.data)

                            if 'subject' == context_field:
                                context_str = 'subject: {0:8}'.format(tup.subject)
                            else:
                                context_str = 'report_id: {0:8}'.format(tup.report_id)

                            print('\t\t_id: ...{0} operation: {1:20} '  \
                                  'nlpql_feature: {2:16} {3} ' \
                                  'data: {4} '.
                                  format(str(tup.oid)[-6:], tup.pipeline_type,
                                         tup.nlpql_feature, context_str,
                                         data_string))
                    elif k == num:
                        print('\t...')
                
        counter += 1
        print()

        # exit if user provided an expression on the command line
        if command_line_expression is not None:
            break

    return True


###############################################################################
def _reduce_expressions(file_data):

    # this needs to be done after token resolution with the name_list
    # also need whitespace between all tokens
    # (expr_eval.is_valid)

    if _TRACE:
        print('called _reduce_expressions...')
    
    task_names = set(file_data.tasks)
    defined_names = set(file_data.names)
    
    expr_dict = OrderedDict()
    for expr_name, expr_def in file_data.expressions:
        expr_dict[expr_name] = expr_def

    all_primitive = False
    while not all_primitive:
        all_primitive = True
        for expr_name, expr_def in expr_dict.items():
            tokens = expr_def.split()
            is_composite = False
            for index, token in enumerate(tokens):
                # only want NLPQL-defined names
                if token not in defined_names:
                    #print('not in defined_names: {0}'.format(token))
                    continue
                elif token in task_names:
                    # cannot reduce further
                    #print('Expression "{0}": primitive name "{1}"'.
                    #      format(expr_name, token))
                    continue
                elif token != expr_name and token in expr_dict:
                    is_composite = True
                    #print('Expression "{0}": composite name "{1}"'.
                    #      format(expr_name, token))
                    # expand and surround with space-separated parens
                    new_token = '( ' + expr_dict[token] + r' )'
                    tokens[index] = new_token
            if is_composite:
                expr_dict[expr_name] = ' '.join(tokens)
                all_primitive = False

    # scan RHS of each expression and ensure expressed entirely in primitives
    primitives = set()
    for expr_name, expr_def in expr_dict.items():
        tokens = expr_def.split()
        for token in tokens:
            if -1 != token.find('.'):
                nlpql_feature, field = token.split('.')
            else:
                nlpql_feature = token
            if token not in defined_names:
                continue
            assert nlpql_feature in task_names
            primitives.add(nlpql_feature)

    assert 0 == len(file_data.reduced_expressions)
    for expr_name, reduced_expr in expr_dict.items():
        file_data.reduced_expressions.append( (expr_name, reduced_expr) )
    assert len(file_data.reduced_expressions) == len(file_data.expressions)
    
    assert 0 == len(file_data.primitives)
    for p in primitives:
        file_data.primitives.append(p)
        
    return file_data


###############################################################################
def _parse_file(filepath):
    """
    Read the NLPQL file and extract the context, nlpql_features, and 
    associated expressions. Returns a FileData namedtuple.
    """

    # repeated whitespace replaced with single space below, so can use just \s
    str_context_statement = r'context\s(?P<context>[^;]+);'
    regex_context_statement = re.compile(str_context_statement)

    str_expr_statement = r'\bdefine\s(final\s)?(?P<feature>[^:]+):\s'  +\
                         r'where\s(?P<expr>[^;]+);'
    regex_expr_statement = re.compile(str_expr_statement, re.IGNORECASE)

    # ClarityNLP task statements have no 'where' clause
    str_task_statement = r'\bdefine\s(final\s)?(?P<feature>[^:]+):\s(?!where)'
    regex_task_statement = re.compile(str_task_statement, re.IGNORECASE)
    
    with open(filepath, 'rt') as infile:
        text = infile.read()

    # strip comments
    text = re.sub(r'//[^\n]+\n', ' ', text)

    # replace newlines with spaces for regex simplicity
    text = re.sub(r'\n', ' ', text)

    # replace repeated spaces with a single space
    text = re.sub(r'\s+', ' ', text)

    # extract the context
    match = regex_context_statement.search(text)
    if match:
        context = match.group('context').strip()
    else:
        print('*** parse_file: context statement not found ***')
        sys.exit(-1)

    # extract expression definitions
    expression_dict = OrderedDict()
    iterator = regex_expr_statement.finditer(text)
    for match in iterator:
        feature = match.group('feature').strip()
        expression = match.group('expr').strip()
        if feature in expression_dict:
            print('*** parse_file: multiple definitions for "{0}" ***'.
                  format(feature))
            sys.exit(-1)
        expression_dict[feature] = expression

    # extract task definitions
    task_list = []
    iterator = regex_task_statement.finditer(text)
    for match in iterator:
        task = match.group('feature').strip()
        if task in task_list:
            print('*** parse_file: multiple definitions for "{0}" ***'.
                  format(task))
            sys.exit(-1)
        task_list.append(task)

    # check task names to ensure not also an expression name
    for t in task_list:
        if t in expression_dict:
            print('*** parse_file: multiple definitions for "{0}" ***'.
                  format(t))
            sys.exit(-1)

    # build list of all names
    name_list = []
    for task_name in task_list:
        name_list.append(task_name)
    for expression_name in expression_dict.keys():
        name_list.append(expression_name)

    file_data = FileData(
        context = context,
        names = name_list,
        primitives = [],   # computed later
        tasks = task_list,
        reduced_expressions = [],
        expressions = [ (expr_name, expr_def) for
                        expr_name,expr_def in expression_dict.items()]
    )

    # reduce the expressions to their most primitive form
    file_data = _reduce_expressions(file_data)

    if _TRACE:
        print('FILE DATA AFTER EXPRESSION REDUCTION: ')
        print('\t    context: {0}'.format(file_data.context))
        print('\t task_names: {0}'.format(file_data.tasks))
        print('\t      names: {0}'.format(file_data.names))
        print('\t primitives: {0}'.format(file_data.primitives))
        expression_count = len(file_data.expressions)
        if 0 == expression_count:
            print('\texpressions: None found')
        else:
            print('\texpressions: ')
            for i in range(len(file_data.expressions)):
                expr_name, expr_def = file_data.expressions[i]
                expr_name, expr_reduced = file_data.reduced_expressions[i]
                print('{0}'.format(expr_name))
                print('\toriginal: {0}'.format(expr_def))
                print('\t reduced: {0}'.format(expr_reduced))

    return file_data
        

###############################################################################
def _get_version():
    return '{0} {1}.{2}'.format(_MODULE_NAME, _VERSION_MAJOR, _VERSION_MINOR)


###############################################################################
if __name__ == '__main__':

    parser = argparse.ArgumentParser(
        description='Run validation tests on the expression evaluator.'
    )

    parser.add_argument('-v', '--version',
                        action='store_true',
                        help='show the version string and exit')
    parser.add_argument('-d', '--debug',
                        action='store_true',
                        help='print debug information to stdout during the run')
    parser.add_argument('-c', '--context',
                        default='patient',
                        help='expression evaluation context, either ' \
                        '"patient" or "document", default is "patient"')
    parser.add_argument('-j', '--jobid',
                        required=True,
                        help='integer job id of a previous ClarityNLP run')
    parser.add_argument('-i', '--isfinal',
                        action='store_true',
                        default=False,
                        help='generate an NLPQL "final" result. Default is to ' \
                        'generate an "intermediate" result.')
    parser.add_argument('-m', '--mongohost',
                        default='localhost',
                        help='IP address of MongoDB host ' \
                        '(default is localhost)')
    parser.add_argument('-p', '--mongoport',
                        default=27017,
                        help='port number for MongoDB host ' \
                        '(default is 27017)')
    parser.add_argument('-n', '--num',
                        default=16,
                        help='number of results to display at start and ' \
                        'end of results array (default is 16)')
    parser.add_argument('-e', '--expr',
                        help='NLPQL expression to evaluate. If this option ' \
                        'is present the -f option cannot be used.')
    parser.add_argument('-f', '--filename',
                        help='NLPQL file to process. If this option is ' \
                        'present the -e option cannot be used.')

    args = parser.parse_args()

    if 'version' in args and args.version:
        print(_get_version())
        sys.exit(0)

    debug = False
    if 'debug' in args and args.debug:
        debug = True
        
    job_id = int(args.jobid)
        
    mongohost = args.mongohost
    mongoport = int(args.mongoport)
    is_final  = args.isfinal
    context   = args.context
    num       = int(args.num)

    expr = None
    if 'expr' in args and args.expr:
        expr = args.expr

    filename = None
    if 'filename' in args and args.filename:
        filename = args.filename

    if expr is not None and filename is not None:
        print('Options -e and -f are mutually exclusive.')
        sys.exit(-1)

    name_list = None
    if filename is not None:
        if not os.path.exists(filename):
            print('File not found: "{0}"'.format(filename))
            sys.exit(-1)

        file_data = _parse_file(filename)
        name_list = file_data.names

    if filename is not None or expr is not None:
        # live test, connect to ClarityNLP mongo collection nlp.phenotype_results
        mongo_client_obj = MongoClient(mongohost, mongoport)
        mongo_db_obj = mongo_client_obj['nlp']
        mongo_collection_obj = mongo_db_obj['phenotype_results']
        
    # delete any data computed from NLPQL expressions, will recompute
    # the task data is preserved
    if filename is not None:
        for nlpql_feature, expression in file_data.expressions:

            result = mongo_collection_obj.delete_many({"job_id":job_id,
                                                       "nlpql_feature":nlpql_feature})
            print('Removed {0} docs with NLPQL feature {1}.'.
                  format(result.deleted_count, nlpql_feature))

    if filename is not None:
        # compute all expressions defined in the NLPQL file
        context = file_data.context
        for nlpql_feature, expression in file_data.expressions:
            _run_tests(job_id,
                       nlpql_feature,
                       expression,
                       context,
                       mongo_collection_obj,
                       num,
                       is_final,
                       name_list,
                       debug)
    elif expr is not None:
        # command-line expression uses the test feature
        final_nlpql_feature = _TEST_NLPQL_FEATURE
    
        _run_tests(job_id,
                   final_nlpql_feature,
                   expr,
                   context,
                   mongo_collection_obj,
                   num,
                   is_final,
                   name_list,
                   debug)        
    else:
        
        # compute the command line expression, if any, or run test suite
        assert run_self_tests(job_id,
                              context,
                              mongohost,
                              mongoport,
                              debug)
        
