# rtl-buddy
# vim: set sw=2:ts=2:et:
#
# Copyright 2024 rtl_buddy contributors
#
import pprint

class TestResults:
  """
  Test results
  """

  def __init__(self, name, results={'result':'NA', 'desc':'NA'}):
    """
    results from vlog_sim.post()
    """
    self.name = name
    self.results = results

    if 'result' not in results:
      results['result'] = 'NA'

    if 'desc' not in results:
      results['desc'] = 'NA'

  def is_pass(self):
    return self.results['result']=='PASS' or self.results['result']=='SKIP'

  def __str__(self):
    return "test_results: "+pprint.pformat(self.results)

class TestPassResults(TestResults):
  """
  Generic test pass results
  """
  
  def __init__(self, name):
    super().__init__(name=name, 
      results={'result':'PASS', 'name':name, 'desc':'Generic test pass'})

class CompileFailResults(TestResults):
  """
  Compilation failed
  """
  
  def __init__(self, name):
    super().__init__(name=name, 
      results={'result':'FAIL', 'name':name, 'desc':'Compile failed'})

class EarlyStopResults(TestResults):
  """
  Early Stopping
  """
  
  def __init__(self, name, desc):
    super().__init__(name=name, 
      results={'result':'NA', 'name':name, 'desc':desc})

class SimTimeoutResults(TestResults):
  """
  Simulation timeout
  """

  def __init__(self, name):
    super().__init__(name=name, 
      results={'result':'FAIL', 'name':name, 'desc':'Sim hit timeout'})

class SkipResults(TestResults):
  """
  Test skipped due to regression level
  """

  def __init__(self, name, desc):
    super().__init__(name=name,
      results={'result':'SKIP', 'name':name, 'desc':desc})


class FilelistFailResults(TestResults):
  """
  Filelist validation failed before compile (bad path, malformed line, missing file, etc.).
  """

  def __init__(self, name, desc):
    super().__init__(name=name,
      results={'result':'FAIL', 'name':name, 'desc':desc})


class SetupFailResults(TestResults):
  """
  Test setup failed before compile/sim.
  """

  def __init__(self, name, desc):
    super().__init__(name=name,
      results={'result':'FAIL', 'name':name, 'desc':desc})
