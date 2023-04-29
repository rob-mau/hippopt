import copy
import dataclasses
from typing import Any, ClassVar

import casadi as cs
import numpy as np

from hippopt.base.optimization_object import (
    OptimizationObject,
    StorageType,
    TOptimizationObject,
)
from hippopt.base.optimization_solver import (
    OptimizationSolver,
    ProblemNotRegisteredException,
    SolutionNotAvailableException,
)
from hippopt.base.parameter import Parameter
from hippopt.base.problem import Problem
from hippopt.base.variable import Variable


@dataclasses.dataclass
class OptiSolver(OptimizationSolver):
    DefaultSolverType: ClassVar[str] = "ipopt"
    _inner_solver: str = dataclasses.field(default=DefaultSolverType)
    problem_type: dataclasses.InitVar[str] = dataclasses.field(default="nlp")

    _options_plugin: dict[str, Any] = dataclasses.field(default_factory=dict)
    _options_solver: dict[str, Any] = dataclasses.field(default_factory=dict)
    options_solver: dataclasses.InitVar[dict[str, Any]] = dataclasses.field(
        default=None
    )
    options_plugin: dataclasses.InitVar[dict[str, Any]] = dataclasses.field(
        default=None
    )

    _cost: cs.MX = dataclasses.field(default=None)
    _solver: cs.Opti = dataclasses.field(default=None)
    _opti_solution: cs.OptiSol = dataclasses.field(default=None)
    _output_solution: TOptimizationObject | list[
        TOptimizationObject
    ] = dataclasses.field(default=None)
    _output_cost: float = dataclasses.field(default=None)
    _variables: TOptimizationObject | list[TOptimizationObject] = dataclasses.field(
        default=None
    )
    _problem: Problem = dataclasses.field(default=None)

    def __post_init__(
        self,
        problem_type: str,
        options_solver: dict[str, Any] = None,
        options_plugin: dict[str, Any] = None,
    ):
        self._solver = cs.Opti(problem_type)
        self._options_solver = (
            options_solver if isinstance(options_solver, dict) else {}
        )
        self._options_plugin = (
            options_plugin if isinstance(options_plugin, dict) else {}
        )
        self._solver.solver(
            self._inner_solver, self._options_plugin, self._options_solver
        )

    def _generate_opti_object(
        self, storage_type: str, name: str, value: StorageType
    ) -> cs.MX:
        if value is None:
            raise ValueError("Field " + name + " is tagged as storage, but it is None.")

        if isinstance(value, np.ndarray):
            if value.ndim > 2:
                raise ValueError(
                    "Field " + name + " has number of dimensions greater than 2."
                )
            if value.ndim < 2:
                value = np.expand_dims(value, axis=1)

        if storage_type is Variable.StorageTypeValue:
            return self._solver.variable(*value.shape)

        if storage_type is Parameter.StorageTypeValue:
            return self._solver.parameter(*value.shape)

        raise ValueError("Unsupported input storage type")

    def _generate_objects_from_instance(
        self, input_structure: TOptimizationObject
    ) -> TOptimizationObject:
        output = copy.deepcopy(input_structure)

        for field in dataclasses.fields(output):
            composite_value = output.__getattribute__(field.name)

            is_list = isinstance(composite_value, list)
            list_of_optimization_objects = is_list and all(
                isinstance(elem, OptimizationObject) or isinstance(elem, list)
                for elem in composite_value
            )

            if (
                isinstance(composite_value, OptimizationObject)
                or list_of_optimization_objects
            ):
                output.__setattr__(
                    field.name, self.generate_optimization_objects(composite_value)
                )
                continue

            if OptimizationObject.StorageTypeField in field.metadata:
                value_list = []
                value_field = dataclasses.asdict(output)[field.name]
                value_list.append(value_field)

                value_list = value_field if is_list else value_list
                output_value = []
                for value in value_list:
                    output_value.append(
                        self._generate_opti_object(
                            storage_type=field.metadata[
                                OptimizationObject.StorageTypeField
                            ],
                            name=field.name,
                            value=value,
                        )
                    )

                output.__setattr__(
                    field.name, output_value if is_list else output_value[0]
                )
                continue

        self._variables = output
        return output

    def _generate_objects_from_list(
        self, input_structure: list[TOptimizationObject]
    ) -> list[TOptimizationObject]:
        assert isinstance(input_structure, list)

        output = copy.deepcopy(input_structure)
        for i in range(len(output)):
            output[i] = self.generate_optimization_objects(output[i])

        self._variables = output
        return output

    def _generate_solution_output(
        self,
        variables: TOptimizationObject
        | list[TOptimizationObject]
        | list[list[TOptimizationObject]],
    ) -> TOptimizationObject | list[TOptimizationObject]:
        output = copy.deepcopy(variables)

        if isinstance(variables, list):
            for i in range(len(variables)):
                output[i] = self._generate_solution_output(variables[i])
            return output

        for field in dataclasses.fields(variables):
            has_storage_field = OptimizationObject.StorageTypeField in field.metadata

            if has_storage_field and (
                (
                    field.metadata[OptimizationObject.StorageTypeField]
                    is Variable.StorageTypeValue
                )
                or (
                    field.metadata[OptimizationObject.StorageTypeField]
                    is Parameter.StorageTypeValue
                )
            ):
                var = variables.__getattribute__(field.name)
                if isinstance(var, list):
                    output_val = []
                    for el in var:
                        output_val.append(np.array(self._opti_solution.value(el)))
                else:
                    output_val = np.array(self._opti_solution.value(var))

                output.__setattr__(field.name, output_val)
                continue

            composite_variable = variables.__getattribute__(field.name)

            is_list = isinstance(composite_variable, list)
            list_of_optimization_objects = is_list and all(
                isinstance(elem, OptimizationObject) or isinstance(elem, list)
                for elem in composite_variable
            )

            if (
                isinstance(composite_variable, OptimizationObject)
                or list_of_optimization_objects
            ):
                output.__setattr__(
                    field.name, self._generate_solution_output(composite_variable)
                )

        return output

    def _set_opti_guess(
        self, storage_type: str, variable: cs.MX, value: np.ndarray
    ) -> None:
        match storage_type:
            case Variable.StorageTypeValue:
                self._solver.set_initial(variable, value)
            case Parameter.StorageTypeValue:
                self._solver.set_value(variable, value)

        return

    def _set_initial_guess_internal(
        self,
        initial_guess: TOptimizationObject
        | list[TOptimizationObject]
        | list[list[TOptimizationObject]],
        corresponding_variable: TOptimizationObject
        | list[TOptimizationObject]
        | list[list[TOptimizationObject]],
        base_name: str = "",
    ) -> None:
        if isinstance(initial_guess, list):
            if not isinstance(corresponding_variable, list):
                raise ValueError(
                    "The input guess is a list, but the specified variable "
                    + base_name
                    + " is not"
                )

            if len(corresponding_variable) != len(initial_guess):
                raise ValueError(
                    "The input guess is a list but the variable "
                    + base_name
                    + " has a different dimension. Expected: "
                    + str(len(corresponding_variable))
                    + " Input: "
                    + str(len(initial_guess))
                )

            for i in range(len(corresponding_variable)):
                self._set_initial_guess_internal(
                    initial_guess=initial_guess[i],
                    corresponding_variable=corresponding_variable[i],
                    base_name=base_name + "[" + str(i) + "].",
                )
            return

        for field in dataclasses.fields(initial_guess):
            guess = initial_guess.__getattribute__(field.name)

            if guess is None:
                continue

            if OptimizationObject.StorageTypeField in field.metadata:
                if not hasattr(corresponding_variable, field.name):
                    raise ValueError(
                        "The guess has the field "
                        + base_name
                        + field.name
                        + " but it is not present in the optimization parameters"
                    )

                corresponding_value = corresponding_variable.__getattribute__(
                    field.name
                )

                if isinstance(corresponding_value, list):
                    if not isinstance(guess, list):
                        raise ValueError(
                            "The guess for the field "
                            + base_name
                            + field.name
                            + " is supposed to be a list."
                        )

                    if len(corresponding_value) == len(guess):
                        raise ValueError(
                            "The guess for the field "
                            + base_name
                            + field.name
                            + " is a list of the wrong size. Expected: "
                            + str(len(corresponding_value))
                            + ". Guess: "
                            + str(len(guess))
                        )

                    for i in range(len(corresponding_value)):
                        if not isinstance(guess[i], np.ndarray):
                            raise ValueError(
                                "The guess for the field "
                                + base_name
                                + field.name
                                + "["
                                + str(i)
                                + "] is not an numpy array."
                            )

                        input_shape = (
                            guess[i].shape
                            if len(guess[i].shape) > 1
                            else (guess[i].shape[0], 1)
                        )

                        if corresponding_value[i].shape != input_shape:
                            raise ValueError(
                                "The dimension of the guess for the field "
                                + base_name
                                + field.name
                                + "["
                                + str(i)
                                + "] does not match with the corresponding optimization variable"
                            )

                        self._set_opti_guess(
                            storage_type=field.metadata[
                                OptimizationObject.StorageTypeField
                            ],
                            variable=corresponding_value[i],
                            value=guess[i],
                        )
                    continue

                if not isinstance(guess, np.ndarray):
                    raise ValueError(
                        "The guess for the field "
                        + base_name
                        + field.name
                        + " is not an numpy array."
                    )

                input_shape = (
                    guess.shape if len(guess.shape) > 1 else (guess.shape[0], 1)
                )

                if corresponding_value.shape != input_shape:
                    raise ValueError(
                        "The guess has the field "
                        + base_name
                        + field.name
                        + " but its dimension does not match with the corresponding optimization variable"
                    )

                self._set_opti_guess(
                    storage_type=field.metadata[OptimizationObject.StorageTypeField],
                    variable=corresponding_value,
                    value=guess,
                )
                continue

            composite_variable_guess = initial_guess.__getattribute__(field.name)

            if isinstance(composite_variable_guess, OptimizationObject):
                if not hasattr(corresponding_variable, field.name):
                    raise ValueError(
                        "The guess has the field "
                        + base_name
                        + field.name
                        + " but it is not present in the optimization structure"
                    )

            self._set_initial_guess_internal(
                initial_guess=composite_variable_guess,
                corresponding_variable=corresponding_variable.__getattribute__(
                    field.name
                ),
                base_name=base_name + field.name + ".",
            )
            continue

    def generate_optimization_objects(
        self, input_structure: TOptimizationObject | list[TOptimizationObject], **kwargs
    ) -> TOptimizationObject | list[TOptimizationObject]:
        if isinstance(input_structure, OptimizationObject):
            return self._generate_objects_from_instance(input_structure=input_structure)
        return self._generate_objects_from_list(input_structure=input_structure)

    def get_optimization_objects(
        self,
    ) -> TOptimizationObject | list[TOptimizationObject]:
        return self._variables

    def register_problem(self, problem: Problem) -> None:
        self._problem = problem

    def get_problem(self) -> Problem:
        if self._problem is None:
            raise ProblemNotRegisteredException
        return self._problem

    def set_initial_guess(
        self, initial_guess: TOptimizationObject | list[TOptimizationObject]
    ) -> None:
        self._set_initial_guess_internal(
            initial_guess=initial_guess, corresponding_variable=self._variables
        )

    def set_opti_options(
        self,
        inner_solver: str = None,
        options_solver: dict[str, Any] = None,
        options_plugin: dict[str, Any] = None,
    ) -> None:
        if inner_solver is not None:
            self._inner_solver = inner_solver
        if options_plugin is not None:
            self._options_plugin = options_plugin
        if options_solver is not None:
            self._options_solver = options_solver

        self._solver.solver(
            self._inner_solver, self._options_plugin, self._options_solver
        )

    def solve(self) -> None:
        self._cost = self._cost if self._cost is not None else cs.MX(0)
        self._solver.minimize(self._cost)
        # TODO Stefano: Consider solution state
        self._opti_solution = self._solver.solve()
        self._output_cost = self._opti_solution.value(self._cost)
        self._output_solution = self._generate_solution_output(self._variables)

    def get_values(self) -> TOptimizationObject | list[TOptimizationObject]:
        if self._output_solution is None:
            raise SolutionNotAvailableException
        return self._output_solution

    def get_cost_value(self) -> float:
        if self._output_cost is None:
            raise SolutionNotAvailableException
        return self._output_cost

    def add_cost(self, input_cost: cs.MX) -> None:
        if self._cost is None:
            self._cost = input_cost
            return

        self._cost += input_cost

    def add_constraint(self, input_constraint: cs.MX) -> None:
        self._solver.subject_to(input_constraint)

    def cost_function(self) -> cs.MX:
        return self._cost
