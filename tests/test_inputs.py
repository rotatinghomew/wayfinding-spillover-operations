from collections import defaultdict
import unittest

from inspect_inputs import read_tables


class SourceRecords(unittest.TestCase):
    def test_assignment_and_observation(self):
        tables = read_tables()
        rounds = {row['round']:row for row in tables['rotation']}
        combinations = defaultdict(set)
        for row in rounds.values():
            combinations[row['day']].add((row['north'], row['east']))
        self.assertTrue(all(len(combo)==4 for combo in combinations.values()))
        for row in tables['parties']:
            plan = rounds[row['round']]
            self.assertLessEqual(int(plan['start']), int(row['entry']))
            self.assertLess(int(row['entry']), int(plan['end']))
            if row['arrival']:
                self.assertLessEqual(int(row['arrival']), int(row['last_seen']))

    def test_revised_passage_distinguishes_two_dispatches(self):
        tables = read_tables()
        first, later = [row for row in tables['links'] if row['link']=='L2']
        self.assertEqual(first['round'], later['round'])
        self.assertGreater(int(first['start']), int(later['start']))
        requests = [row for row in tables['dispatch'] if row['evidence_round']==first['round']]
        self.assertEqual({'planned','started'}, {row['state'] for row in requests})
