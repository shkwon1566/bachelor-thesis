"""The script that generates all 3 datasets and their features"""
import math
import pickle
import pytz
import sys
import json
from imports.timer import Timer
from imports.log import logline
from imports.io import IO, IOInput
from typing import List, TypeVar, Tuple, Union, Dict

import features as features
import numpy as np
import pandas as pd

T = TypeVar('T')

MAX_ROWS = None  # None = infinite

TRAINING_SET_PERCENTAGE = 70
REPORT_SIZE = 50
BATCH_SIZE = 32
MIN_GROUP_SIZE = 150
MIN_GROUP_SIZE = max(MIN_GROUP_SIZE, (BATCH_SIZE * 2) + 2)

io = IO({
    'i': IOInput('/data/s1481096/LosAlamos/data/auth_small.h5', str, arg_name='input_file',
                 descr='The source file for the users (in h5 format)',
                 alias='input_file'),
    'o': IOInput('/data/s1495674/features.p', str, arg_name='output_file',
                 descr='The file to output the features to',
                 alias='output_file'),
    'n': IOInput('auth_small', str, arg_name='name',
                 descr='The name of the h5 file',
                 alias='name'),
    'M': IOInput(False, bool, has_input=False,
                 descr='Enable mega-net mode',
                 alias='meganet')
})


class Row:
    """A row of data"""

    def __init__(self, row: list):
        row_one_split = row[1].split("@")
        row_two_split = row[2].split("@")

        self._row = row
        self.time = pytz.utc.localize(row[0].to_pydatetime()).timestamp()
        self.source_user = self.user = row_one_split[0]
        self.domain = row_one_split[1]
        self.dest_user = row_two_split[0]
        self.src_computer = row[3]
        self.dest_computer = row[4]
        self.auth_type = row[5]
        self.logon_type = row[6]
        self.auth_orientation = row[7]
        self.status = row[8]

    def to_str(self) -> str:
        """Converts the row to a string"""
        return str(self._row)


class PropertyDescription:
    def __init__(self):
        self._list = list()
        self._counts = dict()
        self._unique = 0
        self._freq = 0

    def append(self, item: str):
        """Appends given item to the list of the property"""
        if item not in self._list:
            self._list.append(item)
            self._unique += 1

        self._counts[item] = self._counts.get(item, 0) + 1

    @property
    def unique(self) -> int:
        return len(self._list)

    @property
    def freq(self) -> int:
        highest_index = 0
        for key, value in self._counts.items():
            if value > highest_index:
                highest_index = value

        return highest_index

    @property
    def list(self) -> List[str]:
        return self._list


class Features:
    """All the features fr a model"""

    def __init__(self):
        self._current_access = 0
        self._last_access = 0
        self._domains = PropertyDescription()
        self._dest_users = PropertyDescription()
        self._src_computers = PropertyDescription()
        self._dest_computers = PropertyDescription()
        self._failed_logins = 0
        self._login_attempts = 0

    def update_dest_users(self, user: str):
        """Updates the dest_users list"""
        if user != "?":
            self._dest_users.append(user)

    def update_src_computers(self, computer: str):
        """Updates the src_computers list"""
        if computer != "?":
            self._src_computers.append(computer)

    def update_dest_computers(self, computer: str):
        """Updates the dest_computers list"""
        if computer != "?":
            self._dest_computers.append(computer)

    def update_domains(self, domain: str):
        """Updates the dest_users list"""
        if domain != "?":
            self._domains.append(domain)

    def update(self, row: Row):
        """Updates all data lists for this feature class"""
        self.update_dest_users(row.dest_user)
        self.update_src_computers(row.src_computer)
        self.update_dest_computers(row.dest_computer)
        self.update_domains(row.domain)

        self._last_access = self._current_access
        self._current_access = row.time
        if row.status != 'Success':
            self._failed_logins += 1
        self._login_attempts += 1

    @property
    def last_access(self) -> int:
        """The last time this user has authenticated themselves"""
        return self._last_access

    @property
    def current_access(self) -> int:
        """The timestamp of the current auth operation"""
        return self._current_access

    @property
    def dest_users(self) -> PropertyDescription:
        """All destination users"""
        return self._dest_users

    @property
    def src_computers(self) -> PropertyDescription:
        """All source computers"""
        return self._src_computers

    @property
    def dest_computers(self) -> PropertyDescription:
        """All destination computers"""
        return self._dest_computers

    @property
    def domains(self) -> PropertyDescription:
        """All domains accessed"""
        return self._domains

    @property
    def percentage_failed_logins(self) -> float:
        """The percentage of non-successful logins"""
        return self._failed_logins / self._login_attempts

    def get_time_since_last_access(self) -> int:
        """Gets the time between the current access and the last one"""
        return self._current_access - self._last_access


def normalize_all(feature_list: List[List[float]]) -> np.ndarray:
    np_arr = np.array(feature_list)
    reshaped = np.reshape(feature_list, (np_arr.shape[1], np_arr.shape[0]))

    for col in range(len(reshaped)):
        col_max = max(reshaped[col])
        reshaped[col] = [float(i) / col_max for i in reshaped[col]]

    return np.reshape(np.reshape(np.array(reshaped), (np_arr.shape[0], np_arr.shape[1])),
                      (np_arr.shape[0], np_arr.shape[1]))


def convert_to_features(group) -> np.ndarray:
    """This converts a given group to features"""
    current_features = Features()

    feature_list = list()
    for row in group.itertuples():
        row = Row(row)
        current_features.update(row)
        feature_list.append(features.extract(row, current_features))

    return normalize_all(feature_list)


def closest_multiple(target: int, base: int) -> int:
    lower_bound = target // base
    if float(target - lower_bound) > (base / 2):
        # Round up
        return lower_bound + base
    return lower_bound


def split_list(target: np.ndarray, batch_size: int = 1) -> Union[Tuple[np.ndarray, np.ndarray], None]:
    """This splits given list into a distribution set by the *_SET_PERCENTAGE consts"""
    target_length = len(target)

    # Attempt to account for batch sizes already
    training_set_length = closest_multiple(int(math.ceil(
        (TRAINING_SET_PERCENTAGE / 100) * float(target_length)
    )), batch_size) + 1

    test_set_length = (target_length - 1) - training_set_length
    test_set_length = test_set_length - (test_set_length % batch_size)

    if test_set_length == 0:
        training_set_length -= batch_size
        test_set_length += batch_size

    test_set_length += 1

    if training_set_length <= 1 or test_set_length <= 1:
        return None

    return target[0:training_set_length], target[training_set_length:training_set_length + test_set_length]


def split_dataset(feature_data: np.ndarray) -> Union[Tuple[List[float], List[float]], None]:
    """This converts the dataset to features and splits it into 3 parts"""
    return split_list(feature_data, BATCH_SIZE)


def get_pd_file() -> pd.DataFrame:
    logline('Opening file')
    return pd.read_hdf(io.get('input_file'), io.get('name'), start=0, stop=MAX_ROWS)


def group_df(df: pd.DataFrame) -> pd.DataFrame:
    return df.groupby(df['source_user'].map(lambda source_user: source_user.split('@')[0]), sort=False)


def group_pd_file(f: pd.DataFrame) -> pd.DataFrame:
    logline('Grouping users in file')
    grouped = group_df(f)
    logline('Done grouping users')
    return grouped


def split_dataframe(f: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    # Try to get close to the target split
    training_set = list()
    test_set = list()

    index = 10
    logline('Splitting dataframes')
    grouped = f.groupby(np.arange(len(f)) // (len(f) / 10))
    for g, dataframe in grouped:
        if index <= TRAINING_SET_PERCENTAGE:
            training_set.append(dataframe)
        else:
            test_set.append(dataframe)
        index += 10

    # noinspection PyTypeChecker
    return pd.concat(training_set), pd.concat(test_set)


def get_lower_bound(maximum: int, base: int) -> int:
    return (maximum // base) * base


def gen_meganet_features(f: pd.DataFrame) -> Dict[str, List[Dict[str, Union[str, List[List[float]]]]]]:
    training_set, test_set = split_dataframe(f)

    training_features = list()
    test_features = list()

    del f

    rows = 0

    user_name_list = list()

    logline('Grouping datasets')
    grouped_training = group_df(training_set)
    grouped_test = group_df(test_set)

    training_timer = Timer(len(grouped_training))

    logline('Starting feature generation')
    for name, group in grouped_training:
        if len(group.index) > MIN_GROUP_SIZE:
            user_name = name

            if user_name == "ANONYMOUS LOGON" or user_name == "ANONYMOUS_LOGON":
                continue

            group_features = convert_to_features(group)
            training_features.append(group_features[0:get_lower_bound(len(group_features) - 1, BATCH_SIZE) + 1])

            rows += 1

            user_name_list.append(user_name)

            if rows % REPORT_SIZE == 0:
                logline('At user ', str(rows), '/~', str(len(grouped_training)), ' - ETA for training set is: ' +
                        training_timer.get_eta(),
                        spaces_between=False)
        training_timer.add_to_current(1)

    test_timer = Timer(len(grouped_test))

    rows = 0
    logline('Generating features for test set')
    for name, group in grouped_test:
        if len(group.index) > MIN_GROUP_SIZE:
            user_name = name

            if user_name == "ANONYMOUS LOGON" or user_name == "ANONYMOUS_LOGON" or user_name not in user_name_list:
                continue

            group_features = convert_to_features(group)
            test_features.append({
                "user_name": user_name,
                "dataset": group_features[0:get_lower_bound(len(group_features) - 1, BATCH_SIZE) + 1]
            })
            rows += 1

            if rows % REPORT_SIZE == 0:
                logline('At user ', str(rows), '/~', str(len(grouped_test)), ' - ETA for testing set is: ' +
                        test_timer.get_eta(),
                        spaces_between=False)
        test_timer.add_to_current(1)
    logline('Done generating features')
    logline('Generated features for', len(training_features), 'training set items and', len(test_features),
            'test set items')

    return {
        "training": training_features,
        "test": test_features
    }


def gen_non_meganet_features(f: pd.DataFrame) -> List[Dict[str, Union[str, Dict[str, List[List[float]]]]]]:
    logline('Collecting features')
    users_list = list()

    file_length = len(f)
    timer = Timer(file_length)
    print('File length is', file_length)
    rows = 0
    logline('Starting feature generation')
    for name, group in f:
        if len(group.index) > MIN_GROUP_SIZE:
            user_name = name

            if user_name == "ANONYMOUS LOGON" or user_name == "ANONYMOUS_LOGON":
                continue

            split_dataset_result = split_dataset(convert_to_features(group))
            if split_dataset_result:
                training_set, test_set = split_dataset_result
                user = {
                    "user_name": user_name,
                    "datasets": {
                        "training": training_set,
                        "test": test_set
                    }
                }
                users_list.append(user)

            rows += 1

            if rows % REPORT_SIZE == 0:
                logline('At row ', str(rows), '/~', str(file_length), ' - ETA is: ' + timer.get_eta(),
                        spaces_between=False)
        timer.add_to_current(1)

    del f

    logline("Did a total of", len(users_list), "users")
    logline('Done gathering data')
    return users_list


def get_features() -> Union[Dict[str, List[Dict[str, Union[str, List[List[float]]]]]],
                            List[Dict[str, Union[str, Dict[str, List[List[float]]]]]]]:
    if io.get('meganet'):
        return gen_meganet_features(get_pd_file())
    else:
        f = group_pd_file(get_pd_file())
        return gen_non_meganet_features(f)


def output_data(users_list: List[Dict[str, Union[str, Dict[str, List[List[float]]]]]]):
    logline('Outputting data to file', io.get('output_file'))
    output = open(io.get('output_file'), 'wb')
    try:
        pickle.dump(users_list, output)
    except:
        try:
            logline("Using JSON instead")
            output.write(json.dumps(users_list))
        except:
            logline('Outputting to console instead')
            print(json.dumps(users_list))
            raise
        raise


def main():
    if not io.run:
        return

    logline("Gathering features for", MAX_ROWS if MAX_ROWS is not None else "as many as there are", "rows",
            "using a batch size of", BATCH_SIZE)

    output_data(get_features())
    sys.exit()


if __name__ == "__main__":
    main()
